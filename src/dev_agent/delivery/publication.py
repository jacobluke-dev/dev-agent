"""Authorise and recover draft GitHub publication and observe the published commit.

Preview validates evidence and GitHub destination without mutating Git or GitHub. An explicit
confirmation gates staging, committing, pushing and draft creation. CI observations are retained
separately from approval and verification.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from copy import deepcopy
from pathlib import Path, PurePosixPath

from dev_agent.config import Config
from dev_agent.delivery.adoption import audit_candidate, validate_adoption
from dev_agent.delivery.review import validate_recorded_review
from dev_agent.errors import DevAgentError
from dev_agent.integrations.github import GitHub, GitHubError, ci_observation
from dev_agent.records import RunRecord, Store, atomic_json, atomic_write, new_id, now
from dev_agent.reporting.results import export_results
from dev_agent.repository.git import (
    git,
    output,
    source_state,
    validate_managed_worktree,
    validate_repository,
    validate_unstaged_worktree,
)
from dev_agent.repository.instructions import (
    InstructionBundle,
    audit_files,
    check_documents,
    digest,
    resolve_project,
    safe_path,
    validate_changes,
)
from dev_agent.verification.runs import verification_evidence

DENIED_PARTS = {
    ".git",
    ".aws",
    ".ssh",
    ".codex",
    ".agents",
    ".venv",
    "node_modules",
    "tasks",
    "results",
    "logs",
    "dist",
    "build",
    "__pycache__",
}
SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,})\b|"
    rb"(?im:^\s*['\"]?(?:api[_-]?key|(?:access[_-]?)?token|password|(?:client[_-]?)?secret)"
    rb"['\"]?\s*[:=]\s*['\"]?[^\s'\"]{8,})"
)


def remote_repository(url: str) -> str:
    """Extract owner and repository from a supported credential-free GitHub.com URL."""
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
        url,
    )
    if not match:
        raise DevAgentError(
            "origin must be a credential-free GitHub.com HTTPS or SSH repository URL"
        )
    return match[1]


def validate_origin(worktree: Path, expected: str) -> None:
    """Check fetch and push destinations and reject unsafe Git transport configuration."""
    for key in ("url", "pushurl"):
        result = git(worktree, "config", "--get-all", f"remote.origin.{key}", check=False)
        values = result.stdout.decode().splitlines()
        if (
            result.returncode not in (0, 1)
            or (key == "url" and len(values) != 1)
            or len(values) > 1
        ):
            raise DevAgentError("origin must have exactly one fetch URL and at most one push URL")
        for value in values:
            if remote_repository(value).lower() != expected.lower():
                raise DevAgentError(
                    "origin does not match github.repository; no publication permitted"
                )
    unsafe = git(
        worktree,
        "config",
        "--local",
        "--includes",
        "--get-regexp",
        r"^(url\.|include\.|includeif\.|http\.|credential\.|push\.|core\.sshcommand|remote\..*\.(receivepack|pushoption|mirror))",
        check=False,
    )
    if unsafe.returncode != 1:
        raise DevAgentError(
            "Unsupported local Git transport/rewrite configuration; inspect manually"
        )


def source_content(worktree: Path, audit: RunRecord) -> str:
    """Hash audited tracked and nonignored untracked file evidence independently of HEAD."""
    names = (
        git(worktree, "ls-files", "-z").stdout
        + git(worktree, "ls-files", "--others", "--exclude-standard", "-z").stdout
    )
    return digest(
        {
            os.fsdecode(p): audit[os.fsdecode(p)]
            for p in set(names.split(b"\0")) - {b""}
            if os.fsdecode(p) in audit
        }
    )


def excluded_reason(path: str, root: Path, allowed: tuple[str, ...]) -> str | None:
    """Return why a candidate source path may not be published, or None if allowed.

    Check the operator allowlist, path safety, blocked filenames and file type, size and secret
    heuristics. These checks cannot prove a file is free of secrets.
    """
    safe_path(path)
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    if (
        any(p.lower() in DENIED_PARTS for p in parts)
        or name.startswith(".env")
        or name
        in {
            "credentials",
            "credentials.json",
            "auth.json",
            "config.local.toml",
            "id_rsa",
            "id_ed25519",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            "agent-events.jsonl",
            "agent-output.md",
        }
        or ".local." in name
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".log"))
    ):
        return "credential, local configuration, generated, task or execution artifact"
    if not any(path == p or path.startswith(p.rstrip("/") + "/") for p in allowed):
        return "outside github.allowed_paths"
    candidate = root / path
    if candidate.is_symlink() or not candidate.parent.resolve().is_relative_to(root):
        return "symlink or unsafe parent"
    if candidate.exists():
        if not candidate.is_file() or candidate.stat().st_size > 10 * 1024 * 1024:
            return "special, generated or oversized file"
        content = candidate.read_bytes()
        if b"\0" in content:
            return "binary artifacts require manual publication"
        if SECRET.search(content):
            return "potential secret detected; inspect locally (content withheld)"
    return None


def text_option(value: str, label: str, maximum: int) -> str:
    """Validate bounded operator-supplied publication text and reject likely secret values."""
    if not value.strip() or len(value) > maximum or any(ord(c) < 32 and c != "\n" for c in value):
        raise DevAgentError(f"Invalid {label}; use nonempty text of at most {maximum} characters")
    if SECRET.search(value.encode()):
        raise DevAgentError(f"Potential secret in {label}; content withheld")
    return value


def pull_body(record: RunRecord, plan: RunRecord) -> str:
    """Render a draft PR body with verification status, caveats and a review reminder."""
    profiles = plan["verification"]["profiles"]
    full = profiles["full"]["status"]
    warning = (
        ""
        if full == "passed"
        else (
            "**FULL REGRESSION VERIFICATION IS INCOMPLETE OR FAILED.**\n\n"
            f"Human acknowledgement: {plan['incomplete_full_reason']}\n\n"
        )
    )
    adoption_note = (
        "Legacy implementation adopted. Historical instruction enforcement was unrecorded. "
        "Current scope audit, verification and independent review passed. "
        "The operator explicitly accepted the missing historical provenance.\n\n"
        if plan.get("legacy_adoption")
        else ""
    )
    return (
        f"{adoption_note}"
        f"{warning}## Summary\n\n{plan['summary']}\n\n"
        f"Task: {record['task']}\n\n## Purpose and architecture\n\n"
        "Delivers the approved task requirements. Baseline repository instructions and "
        "changed-file scopes have been validated; behavioural and architectural approval "
        "remains with reviewers.\n\n"
        "## Verification\n\n"
        f"- Targeted: {profiles['targeted']['status']} ({profiles['targeted']['attempt_id']}).\n"
        f"- Full: {full}; this is separate from targeted verification and CI.\n"
        f"- Original implementation outcome: {record['outcome']}.\n"
        f"- Approved source fingerprint: `{plan['verified_fingerprint']}`.\n\n"
        "## Review and limitations\n\n"
        "Review the diff, acceptance criteria, architecture and CI before approval. "
        "A draft PR is not approval. No automatic merging or deployment is requested.\n\n"
        f"dev-agent run: `{record['run_id']}`\n"
    )


def validate_implementation_scopes(
    config: Config, record: RunRecord, bundle: InstructionBundle, audit: RunRecord
) -> RunRecord:
    """Require the normal pre-supply evidence and recheck current implementation scopes."""
    compliance = record.get("instruction_compliance", {})
    if (
        compliance.get("status") != "changed_scopes_validated"
        or compliance.get("failures")
        or any(
            compliance.get(k, {}).get("status") != "validated"
            for k in ("changed_file_validation", "final_changed_file_validation")
        )
    ):
        raise DevAgentError(
            "Mandatory instruction/scope enforcement did not pass or was not recorded"
        )
    if compliance.get("bundle_sha256") != bundle.provenance()["bundle_sha256"]:
        raise DevAgentError("Baseline instruction policy differs from supplied context")
    check_documents(bundle, audit, record["approved_scope"])
    before = Store(config.runs_root).directory(record["run_id"]) / "instruction-audit-before.json"
    if before.is_symlink() or not before.is_file() or before.stat().st_size > 64 * 1024 * 1024:
        raise DevAgentError("Original instruction audit is unavailable")
    validation = validate_changes(
        bundle,
        deepcopy(compliance),
        json.loads(before.read_text()),
        audit,
        record["approved_scope"],
        Path(record["worktree"]),
    )
    if validation["status"] != "validated":
        raise DevAgentError("Current worktree violates mandatory instruction scopes")
    return validation


def publication_plan(
    config: Config,
    record: RunRecord,
    *,
    files: tuple[str, ...] | None = None,
    title: str | None = None,
    summary: str | None = None,
    allow_incomplete_full: bool = False,
    incomplete_full_reason: str | None = None,
    acknowledge_stale_base: bool = False,
) -> tuple[RunRecord, GitHub]:
    """Validate a retained implementation and compute a draft-publication preview.

    Check destination, task branch identity, current instructions, source fingerprint,
    verification and any required delivery review. Resolve manifest, remote status and explicit
    acknowledgements. The supplied record stays canonical while the returned plan describes
    blockers and approval identity.

    Returns:
        The plan and authenticated GitHub client for the configured destination.

    Raises:
        DevAgentError: If eligibility or safe preview prerequisites cannot be established.
    """
    kind = record.get("kind", "implementation")
    if kind not in ("implementation", "adoption") or record.get("cleanup"):
        raise DevAgentError("Publication requires a retained implementation run")
    if (
        record.get("phase") != "finished"
        or record.get("implementation_status") != "agent_completed"
        or record.get("agent", {}).get("exit_code") != 0
    ):
        raise DevAgentError("Codex must have completed successfully before publication")
    project = validate_managed_worktree(config, record)
    if not project.github:
        raise DevAgentError("Configure projects.NAME.github before publication")
    if project.base_branch != record["base_branch"]:
        raise DevAgentError("Configured PR base differs from the original implementation base")
    if record["branch"] in {project.base_branch, "main"}:
        raise DevAgentError("Publication cannot push a baseline branch")
    validate_repository(project, config.runs_root)
    root = Path(record["worktree"])
    baseline = record.get("baseline_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40,64}", baseline):
        raise DevAgentError("Original baseline commit is unknown")
    validate_unstaged_worktree(root)
    current = source_state(root)
    bundle = resolve_project(project, baseline)
    audit = audit_files(root, project.context)
    if kind == "adoption":
        if allow_incomplete_full or incomplete_full_reason:
            raise DevAgentError(
                "Legacy adoption requires passing full verification without override"
            )
        adoption_audit = audit_candidate(project, record, bundle, audit)
        validation = adoption_audit["changes"]
    else:
        validation = validate_implementation_scopes(config, record, bundle, audit)
    content = source_content(root, audit)
    prior = record.get("publication", {})
    owned = prior.get("commit_sha")
    if current["head_commit"] != (owned or baseline):
        raise DevAgentError(
            "Unexpected worktree HEAD; pre-existing/unrecorded commits require manual review"
        )
    fingerprint = current["fingerprint"]
    if owned:
        approved = prior["approved_plan"]
        if (
            content != approved["source_content_sha256"]
            or output(root, "rev-parse", "HEAD^{tree}") != prior["tree_sha"]
        ):
            raise DevAgentError(
                "Source changed after publication; earlier verification/approval does not "
                "certify it"
            )
        fingerprint = approved["verified_fingerprint"]
    legacy_approval = None
    if kind == "adoption":
        legacy_approval = validate_adoption(config, project, record, fingerprint, adoption_audit)
    elif record.get("delivery_id"):
        validate_recorded_review(config, record, fingerprint)
    names = git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
        baseline,
        "--",
    ).stdout
    names += git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
    changed = sorted({os.fsdecode(p) for p in names.split(b"\0") if p})
    if not changed:
        raise DevAgentError("No implementation source changes to publish")
    manifest = []
    excluded = []
    baseline_paths = {
        os.fsdecode(p)
        for p in git(root, "ls-tree", "-r", "--name-only", "-z", baseline).stdout.split(b"\0")
        if p
    }
    untracked = set(
        git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
    )
    for name in changed:
        reason = excluded_reason(name, root, project.github.allowed_paths)
        if reason:
            excluded.append({"path": name, "reason": reason})
        info = audit.get(name)
        manifest.append(
            {
                "path": name,
                "change": "deleted"
                if not info
                else "added"
                if name not in baseline_paths
                else "modified",
                "untracked": os.fsencode(name) in untracked,
                "sha256": info.get("sha256") if info else None,
                "mode": info.get("mode") if info else None,
            }
        )
    evidence = verification_evidence(config, project, record, fingerprint)
    blockers = []
    if evidence["profiles"]["targeted"]["status"] != "passed":
        blockers.append(
            "Passing current targeted verification is required; legacy checks do not imply "
            "targeted coverage"
        )
    if evidence["profiles"]["full"]["status"] != "passed" and not (
        allow_incomplete_full and incomplete_full_reason
    ):
        blockers.append(
            "Full verification is incomplete/failed; use --allow-incomplete-full with "
            "--full-verification-reason"
        )
    if excluded:
        blockers.append(
            "Excluded nonignored changes remain; remove/review them and reverify before publication"
        )
    if files is not None and set(files) != set(changed):
        blockers.append(
            "Approved --file manifest must match all changed source; subset verification is "
            "not established"
        )
    validate_origin(root, project.github.repository)
    client = GitHub(root, project.github.repository)
    account = client.preflight()
    remote_base = client.branch(project.base_branch)
    remote_head = client.branch(record["branch"], missing=True)
    if remote_head and remote_head != owned:
        blockers.append("Remote implementation branch already exists at an unexpected commit")
    stale = remote_base != baseline
    if stale and not acknowledge_stale_base:
        blockers.append(
            "Remote base differs from baseline; review it and use --acknowledge-stale-base "
            "(no rebase/merge)"
        )
    existing = client.existing_pull(record["branch"], project.base_branch)
    if existing and (
        not existing.get("draft")
        or existing.get("state") != "open"
        or existing.get("head", {}).get("sha") != owned
        or existing.get("base", {}).get("ref") != record["base_branch"]
        or existing.get("head", {}).get("repo", {}).get("full_name", "").lower()
        != client.repository.lower()
    ):
        blockers.append("Existing PR is not an open draft at the recorded publication commit")
    plan = {
        "schema_version": 1,
        "run_id": record["run_id"],
        "repository": project.github.repository,
        "account": account,
        "branch": record["branch"],
        "base_branch": project.base_branch,
        "baseline_commit": baseline,
        "remote_base_commit": remote_base,
        "stale_baseline": stale,
        "acknowledge_stale_base": acknowledge_stale_base,
        "source": current,
        "source_content_sha256": content,
        "verified_fingerprint": fingerprint,
        "approved_files": manifest,
        "excluded_files": excluded,
        "ignored_files": [
            os.fsdecode(p)
            for p in git(
                root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
            ).stdout.split(b"\0")
            if p
        ],
        "instruction_validation": validation,
        "verification": evidence,
        "allow_incomplete_full": allow_incomplete_full,
        "incomplete_full_reason": text_option(
            incomplete_full_reason, "full-verification reason", 1000
        )
        if incomplete_full_reason
        else None,
        "title": text_option(
            title
            if title and title.startswith(record["task"] + " — ")
            else f"{record['task']} — {title or 'Approved implementation'}",
            "PR title",
            200,
        ),
        "summary": text_option(
            summary or f"Implements the approved requirements for {record['task']}.",
            "PR summary",
            2000,
        ),
        "draft": True,
        "existing_pr": existing and {"number": existing["number"], "url": existing["html_url"]},
        "workflows": client.workflows(remote_base),
        "blockers": blockers,
        "warning": (
            "Push/PR events may trigger existing workflows, including owner-configured "
            "deployment jobs. Review workflow policy before confirmation."
        ),
    }
    if legacy_approval:
        plan["legacy_adoption"] = legacy_approval
    plan["body"] = pull_body(record, plan)
    # Timestamps and post-commit index/HEAD changes are not approval identity.
    plan["plan_sha256"] = digest(
        {k: v for k, v in plan.items() if k not in ("source", "existing_pr")}
    )
    if source_state(root)["fingerprint"] != current["fingerprint"]:
        raise DevAgentError(
            "Source changed during preview; retry after stopping concurrent writers"
        )
    return plan, client


def persist(config: Config, record: RunRecord) -> None:
    """Save publication evidence and export results, recording export failures canonically."""
    store = Store(config.runs_root)
    store.save(record)
    try:
        export_results(config, record)
    except Exception:
        record["publication"]["export_error"] = "Results export failed; canonical evidence retained"
        store.save(record)
        raise DevAgentError(
            "Publication evidence export failed; inspect canonical record"
        ) from None
    store.save(record)


def push(client: GitHub, branch: str, commit: str) -> None:
    """Push one commit to the task branch without force using the configured GitHub helper.

    A timeout or nonzero exit raises GitHubError. The caller reconciles possible remote
    acceptance before attempting recovery.
    """
    helper = "!" + shlex.quote(client.executable) + " auth git-credential"
    env = client.env | {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    argv = [
        "git",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=" + helper,
        "-c",
        "push.followTags=false",
        "-c",
        "protocol.file.allow=never",
        "push",
        "--porcelain",
        "--no-force",
        f"https://github.com/{client.repository}.git",
        f"{commit}:refs/heads/{branch}",
    ]
    try:
        result = subprocess.run(
            argv,
            cwd=client.worktree,
            env=env,
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except OSError, subprocess.TimeoutExpired:
        raise GitHubError(
            "Push could not complete; remote may have accepted it; rerun confirmed "
            "publication to reconcile"
        ) from None
    if result.returncode:
        raise GitHubError(
            "Push failed; committed source retained; no automatic retry or force push"
        )


def validate_staging(root: Path, manifest: list[RunRecord]) -> None:
    """Require the staged file set, content hashes and modes to match the reviewed manifest."""
    staged = {
        os.fsdecode(p)
        for p in git(
            root,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            "--",
        ).stdout.split(b"\0")
        if p
    }
    if staged != {f["path"] for f in manifest}:
        raise DevAgentError("Staged file list does not exactly match the approved manifest")
    for item in manifest:
        if item["sha256"] is not None:
            content = git(root, "show", ":" + item["path"]).stdout
            mode = git(root, "ls-files", "--stage", "--", ":(literal)" + item["path"]).stdout.split(
                b" ", 1
            )[0]
            expected = b"100755" if item["mode"] & 0o111 else b"100644"
            if hashlib.sha256(content).hexdigest() != item["sha256"] or mode != expected:
                raise DevAgentError(
                    "Staged content differs from approved source; inspect attributes/filters"
                )


def publish_run(
    config: Config,
    run_id: str,
    *,
    confirmed: bool = False,
    dry_run: bool = False,
    plan_sha256: str | None = None,
    watch: bool = True,
    **options: object,
) -> RunRecord:
    """Preview or execute publication for a retained implementation run.

    Unconfirmed calls and dry runs compute the plan without Git or remote writes. Confirmed
    calls hold the run lock, recheck the manifest and any supplied plan digest, then reuse or
    create the commit and the draft PR in recorded steps. Keep evidence of partial publication
    and observe CI for the published commit.

    Returns:
        A preview envelope or the updated canonical implementation record.
    """
    store = Store(config.runs_root)
    if not confirmed or dry_run:
        plan, _ = publication_plan(config, store.load(run_id), **options)
        return {"dry_run": dry_run, "confirmed": False, "plan": plan}
    with store.lock_candidate(run_id):
        record = store.load(run_id)
        plan, client = publication_plan(config, record, **options)
        if plan_sha256 and plan_sha256 != plan["plan_sha256"]:
            raise DevAgentError("Publication plan changed since review; preview and approve again")
        if plan["blockers"]:
            raise DevAgentError("Publication blocked: " + "; ".join(plan["blockers"]))
        pub = record.setdefault(
            "publication", {"schema_version": 1, "events": [], "ci_observations": []}
        )
        attempt_id = new_id("publish", record["task"])
        directory = store.directory(run_id) / "publication" / attempt_id
        directory.mkdir(parents=True, mode=0o700)
        atomic_json(directory / "plan.json", plan)
        atomic_json(directory / "approved-files.json", plan["approved_files"])
        atomic_write(directory / "pr-body.md", plan["body"].encode())
        pub.setdefault("plans", []).append(
            {"attempt_id": attempt_id, "plan_sha256": plan["plan_sha256"], "approved_at": now()}
        )
        for key in ("error", "failure_phase", "recovery", "export_error"):
            pub.pop(key, None)
        pub.update(
            approved_plan=plan,
            repository=client.repository,
            branch=record["branch"],
            base_branch=record["base_branch"],
            status="approved",
        )
        phase = "commit"

        def event(status: str) -> None:
            """Persist a timestamped publication event for recovery of the current attempt."""
            pub["status"] = status
            pub["events"].append({"status": status, "at": now(), "attempt_id": attempt_id})
            persist(config, record)

        event("approved")
        try:
            root = Path(record["worktree"])
            if not pub.get("commit_sha"):
                if source_state(root)["fingerprint"] != plan["source"]["fingerprint"]:
                    raise DevAgentError("Source changed after approval")
                paths = (
                    b"\0".join((":(literal)" + f["path"]).encode() for f in plan["approved_files"])
                    + b"\0"
                )
                git(root, "add", "--pathspec-from-file=-", "--pathspec-file-nul", input=paths)
                validate_staging(root, plan["approved_files"])
                if (
                    source_content(
                        root, audit_files(root, config.project(record["project"]).context)
                    )
                    != plan["source_content_sha256"]
                ):
                    raise DevAgentError("Source changed during staging")
                pub["tree_sha"] = output(root, "write-tree")
                event("staged")
                # Explicit identity comes from local Git config, never from task text.
                git(root, "-c", "commit.gpgsign=false", "commit", "-m", plan["title"])
                pub["commit_sha"] = output(root, "rev-parse", "HEAD")
                if (
                    output(root, "rev-parse", "HEAD^{tree}") != pub["tree_sha"]
                    or output(root, "rev-parse", "HEAD^") != record["baseline_commit"]
                ):
                    raise DevAgentError(
                        "Committed tree or parent differs from approved publication"
                    )
                event("committed")
            phase = "push"
            validate_managed_worktree(config, record)
            if (
                output(root, "rev-parse", "HEAD") != pub["commit_sha"]
                or source_content(
                    root, audit_files(root, config.project(record["project"]).context)
                )
                != plan["source_content_sha256"]
            ):
                raise DevAgentError("Source or HEAD changed before push; publication stopped")
            validate_origin(root, client.repository)
            head = client.branch(record["branch"], missing=True)
            if head is not None and head != pub["commit_sha"]:
                raise DevAgentError("Remote head changed; refusing to push")
            if head is None:
                push(client, record["branch"], pub["commit_sha"])
            if client.branch(record["branch"]) != pub["commit_sha"]:
                raise DevAgentError("Remote branch commit could not be confirmed")
            event("pushed")
            phase = "pull_request"
            pull = client.existing_pull(record["branch"], record["base_branch"])
            if pull is None:
                client.call(
                    "pr",
                    "create",
                    "--repo",
                    client.repository,
                    "--draft",
                    "--base",
                    record["base_branch"],
                    "--head",
                    record["branch"],
                    "--title",
                    plan["title"],
                    "--body-file",
                    str(directory / "pr-body.md"),
                )
                pull = client.existing_pull(record["branch"], record["base_branch"])
            if (
                not pull
                or not pull.get("draft")
                or pull.get("state") != "open"
                or pull.get("head", {}).get("sha") != pub["commit_sha"]
                or pull.get("base", {}).get("ref") != record["base_branch"]
                or pull.get("head", {}).get("repo", {}).get("full_name", "").lower()
                != client.repository.lower()
            ):
                raise DevAgentError(
                    "Created/existing PR identity or draft status could not be confirmed"
                )
            pub.update(
                pr_number=pull["number"], pr_url=pull["html_url"], draft=True, published_at=now()
            )
            event("draft_created")
            phase = "ci"
            monitor(config, record, client, watch=watch)
            event("published")
        except (Exception, KeyboardInterrupt) as exc:
            pub.update(
                status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                failure_phase=phase,
                error="Publication interrupted"
                if isinstance(exc, KeyboardInterrupt)
                else str(exc)
                if isinstance(exc, GitHubError)
                else "Local publication step failed; inspect worktree and canonical evidence",
                recovery=(
                    "Resolve the reported problem, preview again, then repeat publish --draft "
                    "--confirm with the same verification acknowledgements; "
                    "no reset, force push or duplicate PR is automatic"
                ),
            )
            pub["events"].append(
                {
                    "status": pub["status"],
                    "phase": phase,
                    "at": now(),
                    "attempt_id": attempt_id,
                    "error": pub["error"],
                    "recovery": pub["recovery"],
                }
            )
            persist(config, record)
            if isinstance(exc, KeyboardInterrupt):
                raise
        return record


def monitor(config: Config, record: RunRecord, client: GitHub, *, watch: bool) -> RunRecord:
    """Append bounded CI observations for the recorded published SHA.

    Watch mode polls eligible incomplete states until completion or the monitoring deadline. A
    timeout records a local outcome without cancelling remote workflow jobs.
    """
    pub = record["publication"]
    settings = config.project(record["project"]).github
    deadline = time.monotonic() + settings.ci_timeout_seconds
    client.deadline = deadline
    try:
        workflows = client.workflows(pub["commit_sha"])
    except GitHubError:
        observation = {
            "observed_at": now(),
            "commit_sha": pub["commit_sha"],
            "checks": [],
            "status": "monitoring_timeout"
            if time.monotonic() >= deadline
            else "unable_to_retrieve",
            "error": "Workflow inventory could not be retrieved; CI is not qualified",
        }
        pub["ci_observations"].append(observation)
        persist(config, record)
        return observation
    pub["workflows"] = workflows
    while True:
        observation = ci_observation(client, pub["commit_sha"], pub["pr_number"], workflows)
        pub["ci_observations"].append(observation)
        persist(config, record)
        deadline_expired = time.monotonic() >= deadline
        if (
            not watch
            or observation["status"] not in ("pending", "no_checks", "unable_to_retrieve")
            or (observation["status"] == "unable_to_retrieve" and not deadline_expired)
        ):
            return observation
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            observation = {
                "observed_at": now(),
                "commit_sha": pub["commit_sha"],
                "status": "monitoring_timeout",
                "checks": [],
                "note": (
                    "Remote workflows were not cancelled; "
                    "previous observations retain check details"
                ),
            }
            pub["ci_observations"].append(observation)
            persist(config, record)
            return observation
        time.sleep(min(settings.poll_interval_seconds, remaining))


def ci_run(config: Config, run_id: str, *, watch: bool = False) -> RunRecord:
    """Validate a recorded publication and observe its PR and exact-commit checks.

    Hold the run lock and append observations. Missing publication evidence or an incompatible
    destination raises DevAgentError. GitHub preflight failure is stored as unavailable
    evidence.
    """
    store = Store(config.runs_root)
    with store.lock_candidate(run_id):
        record = store.load(run_id)
        pub = record.get("publication", {})
        if not pub.get("pr_number") or not pub.get("commit_sha"):
            raise DevAgentError("No published PR and commit are recorded for this run")
        project = validate_managed_worktree(config, record)
        if not project.github or project.github.repository != pub["repository"]:
            raise DevAgentError("Publication destination differs from current configuration")
        try:
            client = GitHub(Path(record["worktree"]), pub["repository"])
            client.preflight()
        except GitHubError as exc:
            observation = {
                "observed_at": now(),
                "commit_sha": pub["commit_sha"],
                "status": "unable_to_retrieve",
                "checks": [],
                "error": str(exc),
            }
            pub["ci_observations"].append(observation)
            persist(config, record)
            return observation
        return monitor(config, record, client, watch=watch)
