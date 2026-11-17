"""Provide a bounded GitHub CLI client and normalise commit-specific CI evidence.

This trusted integration has its own authentication environment. Delivery supplies repository
identity and decides whether any write command is authorised.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from dev_agent.errors import DevAgentError
from dev_agent.process import environment
from dev_agent.records import RunRecord, now


class GitHubError(DevAgentError):
    """Report a GitHub CLI or response failure without raw external diagnostics."""

    pass


class GitHub:
    """Bind a GitHub CLI session to a worktree and configured repository.

    The client captures approved authentication environment variables in memory and disables
    prompts. An optional shared deadline bounds calls during CI polling.
    """

    def __init__(self, worktree: Path, repository: str) -> None:
        """Resolve gh and assemble the trusted GitHub subprocess environment."""
        self.worktree = worktree
        self.repository = repository
        self.executable = shutil.which("gh", path=environment().get("PATH"))
        if not self.executable:
            raise GitHubError(
                "GitHub CLI is missing; install gh and authenticate outside dev-agent"
            )
        self.deadline: float | None = None
        self.env = environment() | {
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "GH_PAGER": "cat",
            "NO_COLOR": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        # This environment is used only by trusted gh/Git, never Codex or project checks.
        for key in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "GH_TOKEN", "GITHUB_TOKEN"):
            if key in os.environ:
                self.env[key] = os.environ[key]

    def call(self, *args: str, missing: bool = False) -> str | None:
        """Run a bounded gh subprocess and return decoded stdout.

        With missing=True an HTTP 404 becomes None. Other command failures raise GitHubError
        without including captured stderr, which may contain credentials.
        """
        timeout = 60.0 if self.deadline is None else min(60.0, self.deadline - time.monotonic())
        if timeout <= 0:
            raise GitHubError("GitHub monitoring deadline elapsed")
        try:
            result = subprocess.run(
                [self.executable, *args],
                cwd=self.worktree,
                env=self.env,
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except OSError, subprocess.TimeoutExpired:
            raise GitHubError(f"GitHub {args[0]} could not complete; no automatic retry") from None
        if missing and result.returncode and b"HTTP 404" in result.stderr:
            return None
        if result.returncode:
            # External diagnostics may contain headers/tokens. Never export them verbatim.
            raise GitHubError(
                f"GitHub {args[0]} failed (exit {result.returncode}); inspect gh locally"
            )
        if len(result.stdout) > 8 * 1024 * 1024:
            raise GitHubError("GitHub response exceeds the safe response limit")
        return result.stdout.decode("utf-8")

    def api(self, suffix: str, *, missing: bool = False) -> object:
        """Decode a repository-relative GitHub GET response, preserving an allowed 404."""
        result = self.call(
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            f"repos/{self.repository}/{suffix}",
            missing=missing,
        )
        try:
            return json.loads(result) if result is not None else None
        except ValueError:
            raise GitHubError("GitHub returned invalid JSON") from None

    def preflight(self) -> str:
        """Check authentication, push permissions and repository state and return the login."""
        self.call("auth", "status", "--hostname", "github.com")
        try:
            account = json.loads(self.call("api", "--hostname", "github.com", "user"))["login"]
            repo = self.api("")
            if repo["full_name"].lower() != self.repository.lower():
                raise GitHubError("GitHub repository does not match the configured destination")
            if not repo.get("permissions", {}).get("push"):
                raise GitHubError("Authenticated GitHub account lacks push permission")
            if repo.get("archived") or repo.get("disabled"):
                raise GitHubError("GitHub repository is archived or disabled")
            return account
        except KeyError, TypeError, ValueError:
            raise GitHubError("Cannot establish GitHub account/repository permissions") from None

    def branch(self, name: str, *, missing: bool = False) -> str | None:
        """Read a branch's remote commit SHA and optionally allow a missing branch."""
        data = self.api(f"git/ref/heads/{quote(name, safe='')}", missing=missing)
        if data is None:
            return None
        sha = data.get("object", {}).get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise GitHubError("GitHub returned an invalid branch commit")
        return sha

    def pull(self, number: int) -> RunRecord:
        """Retrieve one pull request by number from the configured repository."""
        return self.api(f"pulls/{int(number)}")

    def existing_pull(self, branch: str, base: str) -> RunRecord | None:
        """Find a unique PR for the head branch across states.

        The caller validates the returned base and draft state. The base argument is retained by
        this interface but is not used to narrow the API query.
        """
        head = self.repository.split("/")[0] + ":" + branch
        pulls = self.api(f"pulls?state=all&head={quote(head, safe='')}&per_page=100")
        if not isinstance(pulls, list) or len(pulls) > 1:
            raise GitHubError("Ambiguous existing PRs; inspect GitHub manually")
        return pulls[0] if pulls else None

    def workflows(self, commit: str) -> list[RunRecord]:
        """Inventory workflow YAML and record content hashes at the supplied commit.

        The inventory is bounded and identifies events conservatively. It does not evaluate the
        workflow or trigger any job.
        """
        listing = self.api(f"contents/.github/workflows?ref={commit}", missing=True)
        if listing is None:
            return []
        if not isinstance(listing, list) or len(listing) > 100:
            raise GitHubError("Cannot safely inventory GitHub Actions workflows")
        result = []
        for item in listing:
            path = item.get("path", "")
            if not re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path):
                continue
            data = self.api(f"contents/{quote(path, safe='/')}?ref={commit}")
            try:
                raw = base64.b64decode(data["content"], validate=False)
                if len(raw) > 1024 * 1024:
                    raise ValueError
                text = raw.decode("utf-8")
            except KeyError, ValueError, UnicodeError:
                raise GitHubError("Cannot inspect workflow content safely") from None
            result.append(
                {
                    "path": path,
                    "commit": commit,
                    "content_sha256": hashlib.sha256(raw).hexdigest(),
                    **workflow_triggers(text),
                }
            )
        return result


def workflow_triggers(text: str) -> RunRecord:
    """Extract likely workflow triggers from bounded YAML text.

    Return a conservative indicator for unknown syntax or conditions. This static approximation
    is not a YAML parser or a prediction that a workflow will run.
    """
    match = re.search(r"(?m)^(?:on|'on'|\"on\")[ \t]*:[ \t]*([^\n]*)(?:\n|$)", text)
    if not match:
        return {"events": [], "trigger_analysis": "unknown", "conditions_require_review": True}
    inline = match[1].split("#", 1)[0].strip()
    if inline:
        simple = re.fullmatch(r"\[?\s*([\w, '\"-]+)\s*\]?", inline)
        events = re.findall(
            r"\b(?:pull_request_target|pull_request|push|workflow_dispatch)\b", inline
        )
        known = bool(simple) and bool(events)
    else:
        block = re.split(r"(?m)^\S", text[match.end() :], maxsplit=1)[0]
        entries = re.findall(r"(?m)^( +)([A-Za-z_]+)\s*:", block)
        indent = min((len(i) for i, _ in entries), default=0)
        events = [name for spaces, name in entries if len(spaces) == indent]
        known = bool(events)
    return {
        "events": sorted(set(events)),
        "trigger_analysis": "static" if known else "unknown",
        "conditions_require_review": bool(re.search(r"(?m)^\s*if\s*:", text)) or not known,
        "draft_condition_present": bool(re.search(r"pull_request\.draft|isDraft", text)),
        "note": (
            "Static inventory only; branch/path filters, conditions and repository policy "
            "may prevent runs"
        ),
    }


def ci_observation(client: GitHub, commit: str, pr: int, workflows: list[RunRecord]) -> RunRecord:
    """Describe check runs and statuses for the exact published commit.

    Check the PR head first. Normalise GitHub states and bounded pagination into an observation
    without treating missing, mismatched or unavailable checks as success.
    """
    observation = {
        "observed_at": now(),
        "commit_sha": commit,
        "checks": [],
        "status": "unable_to_retrieve",
    }
    try:
        pull = client.pull(pr)
        observation["pr_head_sha"] = pull["head"]["sha"]
        if pull["head"]["sha"] != commit:
            observation.update(
                status="commit_mismatch", error="PR head changed; previous CI does not certify it"
            )
            return observation
        checks = []
        for kind in ("check-runs", "statuses"):
            for page in range(1, 11):
                data = client.api(f"commits/{commit}/{kind}?per_page=100&page={page}")
                batch = data["check_runs"] if kind == "check-runs" else data
                if not isinstance(batch, list):
                    raise GitHubError("Invalid CI response")
                for check in batch:
                    if kind == "check-runs":
                        if check.get("head_sha", commit) != commit:
                            raise GitHubError("Check run commit differs from requested commit")
                        conclusion = check.get("conclusion")
                        state = (
                            "pending"
                            if check.get("status") != "completed"
                            else {
                                "success": "passed",
                                "failure": "failed",
                                "timed_out": "failed",
                                "action_required": "failed",
                                "startup_failure": "failed",
                                "cancelled": "cancelled",
                                "skipped": "skipped",
                                "neutral": "skipped",
                                "stale": "failed",
                            }.get(conclusion, "unable_to_retrieve")
                        )
                        checks.append(
                            {
                                "name": check["name"],
                                "status": state,
                                "state": check.get("status"),
                                "conclusion": conclusion,
                                "url": check.get("html_url"),
                                "started_at": check.get("started_at"),
                                "completed_at": check.get("completed_at"),
                                "source": "check_run",
                            }
                        )
                    else:
                        # Statuses are newest-first: only the latest value per context is current.
                        if any(
                            c["source"] == "status" and c["name"] == check["context"]
                            for c in checks
                        ):
                            continue
                        checks.append(
                            {
                                "name": check["context"],
                                "status": {
                                    "success": "passed",
                                    "failure": "failed",
                                    "error": "failed",
                                    "pending": "pending",
                                }.get(check["state"], "unable_to_retrieve"),
                                "state": check["state"],
                                "url": check.get("target_url"),
                                "started_at": check.get("created_at"),
                                "completed_at": check.get("updated_at"),
                                "source": "status",
                            }
                        )
                if len(batch) < 100:
                    break
            else:
                raise GitHubError("CI pagination exceeds safe limits; status is incomplete")
        states = {c["status"] for c in checks}
        status = next(
            (
                s
                for s in ("unable_to_retrieve", "pending", "failed", "cancelled", "skipped")
                if s in states
            ),
            "passed",
        )
        if not checks:
            status = "no_checks_configured" if not workflows else "no_checks"
        observation.update(checks=checks, status=status)
    except DevAgentError, KeyError, TypeError, ValueError:
        observation.update(
            status="unable_to_retrieve", error="Cannot establish complete commit-specific CI status"
        )
    return observation
