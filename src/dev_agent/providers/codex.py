"""Run workspace-write Codex through its non-interactive CLI.

Implementation supplies an isolated Git worktree, prompt and log paths. This adapter sets its
own restrictive flags and interprets completion events before the workflow evaluates source
changes.
"""

import json
import shutil
import subprocess
from pathlib import Path

from dev_agent.config import AgentConfig
from dev_agent.errors import DevAgentError
from dev_agent.process import ProcessResult, environment, execute

SANDBOX = "workspace-write"
SAFETY_SETTINGS: dict[str, object] = {
    "approval_policy": "never",
    "sandbox_workspace_write.network_access": False,
    "sandbox_workspace_write.writable_roots": [],
    "sandbox_workspace_write.exclude_tmpdir_env_var": True,
    "sandbox_workspace_write.exclude_slash_tmp": True,
    "web_search": "disabled",
    "allow_login_shell": False,
    "shell_environment_policy.inherit": "core",
    "shell_environment_policy.ignore_default_excludes": False,
    "project_doc_max_bytes": 0,
    "features.hooks": False,
    "features.multi_agent": False,
    "features.multi_agent_v2": False,
    "agents.enabled": False,
    "features.apps": False,
    "features.plugins": False,
    "features.memories": False,
    "features.memory_tool": False,
}


class CodexRunner:
    """Manage Codex preflight and one bounded process invocation at a time.

    The adapter remembers the executable resolved during preflight. Its completion check
    concerns CLI evidence. Verification and publication are owned by the workflow.
    """

    additional_required_flags: tuple[str, ...] = ()

    def __init__(self) -> None:
        """Initialise an adapter with no resolved executable."""
        self.executable: str | None = None

    def preflight(self) -> dict[str, object]:
        """Resolve Codex and check that its help advertises the required safety flags.

        Return runtime version and sandbox provenance. Missing tools, unsupported flags and
        failed or timed-out probes raise DevAgentError. The probe does not verify account access
        or run a model.
        """
        self.executable = shutil.which("codex", path=environment().get("PATH", ""))
        if not self.executable:
            raise DevAgentError(
                "Codex CLI is not installed or not on PATH; install it and run codex login"
            )
        try:
            help_result = subprocess.run(
                [self.executable, "exec", "--help"],
                capture_output=True,
                text=True,
                timeout=15,
                env=environment(),
            )
            version = subprocess.run(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                env=environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DevAgentError(f"Cannot inspect Codex CLI: {exc}") from exc
        required = (
            "--json",
            "--output-last-message",
            "--sandbox",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            *self.additional_required_flags,
        )
        if (
            help_result.returncode
            or version.returncode
            or any(flag not in help_result.stdout for flag in required)
        ):
            raise DevAgentError(
                "Codex lacks required safety/exec flags; upgrade Codex CLI (tested: 0.169.0)"
            )
        return {
            "provider": "codex",
            "version": version.stdout.strip(),
            "executable": self.executable,
            "sandbox": SANDBOX,
            "safety_overrides": SAFETY_SETTINGS,
        }

    def command(self, worktree: Path, directory: Path, config: AgentConfig) -> list[str]:
        """Build the restricted exec invocation with the supplied workspace and output paths.

        Requires completed preflight. The argv reserves stdin for prompt text and sets explicit
        workspace, approval, integration and shell-networking restrictions.
        """
        if not self.executable:
            raise DevAgentError("Codex preflight must run before execution")
        # CLI overrides have precedence. Untrusted projects skip repository config, while
        # --ignore-user-config retains authentication without inheriting user integrations.
        settings = SAFETY_SETTINGS | {
            f"projects.{json.dumps(str(worktree))}.trust_level": "untrusted",
        }
        if config.reasoning_effort:
            settings["model_reasoning_effort"] = config.reasoning_effort
        argv = [
            self.executable,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--sandbox",
            SANDBOX,
            "--json",
            "--color",
            "never",
            "--cd",
            str(worktree),
            "--output-last-message",
            str(directory / "final-response.md"),
        ]
        for key, value in settings.items():
            argv.extend(["-c", f"{key}={json.dumps(value)}"])
        if config.model:
            argv.extend(["--model", config.model])
        argv.append("-")
        return argv

    def run(
        self, worktree: Path, prompt: str, directory: Path, config: AgentConfig
    ) -> ProcessResult:
        """Send the prompt over stdin and capture events, stderr and the final response.

        Return ProcessResult evidence. A zero exit still fails if the event stream lacks
        accepted completion, contains failure evidence or the final response is missing.
        """
        result = execute(
            self.command(worktree, directory, config),
            cwd=worktree,
            stdout=directory / "agent-events.jsonl",
            stderr=directory / "agent.stderr.log",
            timeout=config.timeout_seconds,
            stdin=prompt,
        )
        # Exit 0 alone is not completion evidence. Require a completed turn and final response.
        if result.ok:
            completed = False
            failed = False
            try:
                with (directory / "agent-events.jsonl").open(encoding="utf-8") as stream:
                    for line in stream:
                        event = json.loads(line)
                        completed |= event.get("type") == "turn.completed"
                        failed |= event.get("type") in ("turn.failed", "error")
                final = directory / "final-response.md"
                if not completed or failed or not final.is_file() or not final.stat().st_size:
                    raise ValueError("Missing completion event/final response or failed event")
            except (OSError, ValueError, AttributeError) as exc:
                return ProcessResult(
                    result.exit_code, result.duration_seconds, error=f"Invalid Codex result: {exc}"
                )
        return result
