"""Run Codex in the read-only investigation and review boundary.

The adapter adds a Bubblewrap mount and PID namespace to the standard Codex restrictions. It
binds snapshots and input files read-only and gives the process a dedicated output mount.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from dev_agent.config import AgentConfig
from dev_agent.errors import DevAgentError, IsolationError
from dev_agent.process import environment
from dev_agent.providers.codex import CodexRunner


class CodexInvestigationRunner(CodexRunner):
    """Hold the preflight state for a read-only native Codex invocation.

    The transient namespace exposes the required runtime and existing auth.json while masking
    the original checkout and unrelated home contents.
    """

    additional_required_flags = ("--skip-git-repo-check", "--output-schema")

    def __init__(self) -> None:
        """Initialise Codex discovery and namespace prerequisites as unresolved."""
        super().__init__()
        self.bwrap: str | None = None
        self.auth: Path | None = None

    def namespace(
        self, command: list[str], snapshot: Path | None = None, directory: Path | None = None
    ) -> list[str]:
        """Build Bubblewrap argv for a runtime probe or sealed-snapshot execution.

        The optional snapshot and output directory enable repository and input mounts. Without
        both, the command runs with the minimal runtime mounts only. Missing preflight state
        raises IsolationError.
        """
        if not self.bwrap or not self.executable or not self.auth:
            raise IsolationError("Read-only investigation preflight has not completed")
        argv = [
            self.bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--clearenv",
        ]
        for path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(path).exists():
                argv += ["--ro-bind", path, path]
        for path in (
            "/etc/ssl/certs",
            "/etc/pki/tls/certs",
            "/etc/pki/ca-trust/extracted",
            "/etc/resolv.conf",
            "/etc/hosts",
            "/etc/nsswitch.conf",
            "/etc/ld.so.cache",
        ):
            if Path(path).exists():
                argv += ["--ro-bind", path, path]
        argv += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/home",
            "--tmpfs",
            "/home/investigator",
            "--dir",
            "/home/investigator/.codex",
            "--dir",
            "/agent",
            "--ro-bind",
            str(Path(self.executable).resolve()),
            "/agent/codex",
            "--ro-bind",
            str(self.auth),
            "/home/investigator/.codex/auth.json",
            "--setenv",
            "HOME",
            "/home/investigator",
            "--setenv",
            "USER",
            "investigator",
            "--setenv",
            "LOGNAME",
            "investigator",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "TERM",
            "dumb",
            "--setenv",
            "CODEX_HOME",
            "/home/investigator/.codex",
        ]
        if snapshot and directory:
            argv += [
                "--ro-bind",
                str(snapshot),
                "/workspace",
                "--dir",
                "/input",
                "--bind",
                str(directory),
                "/output",
            ]
            for name in ("requirements-snapshot.json", "context.json", "investigation-schema.json"):
                argv += ["--ro-bind", str(directory.parent / name), f"/input/{name}"]
        argv += ["--remount-ro", "/", "--chdir", "/workspace" if snapshot else "/", "--", *command]
        return argv

    def preflight(self) -> dict[str, object]:
        """Check the base Codex interface, Bubblewrap and the existing auth.json.

        Run the native executable's version probe inside the namespace. Return isolation
        provenance or raise IsolationError before launching the investigation agent.
        """
        try:
            metadata = super().preflight()
        except DevAgentError as exc:
            raise IsolationError(str(exc)) from None
        self.bwrap = shutil.which("bwrap", path=environment().get("PATH", ""))
        if not self.bwrap:
            raise IsolationError(
                "Investigation requires Bubblewrap (bwrap); unrestricted execution is refused"
            )
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self.auth = home.expanduser().resolve() / "auth.json"
        if not self.auth.is_file() or self.auth.is_symlink():
            raise IsolationError(
                "Investigation requires existing file-based Codex auth.json; "
                "authenticate outside dev-agent"
            )
        try:
            probe = subprocess.run(
                self.namespace(["/agent/codex", "--version"]),
                env=environment(),
                capture_output=True,
                timeout=15,
            )
        except OSError, subprocess.TimeoutExpired:
            raise IsolationError(
                "Read-only namespace preflight failed; no agent session started"
            ) from None
        if probe.returncode:
            raise IsolationError(
                "Bubblewrap/native Codex namespace is unavailable; enable user "
                "namespaces or use a suitable Linux host"
            )
        metadata.update(
            mode="investigation",
            sandbox="read-only",
            isolation="bubblewrap-mount-pid",
            authentication="existing Codex auth.json mounted read-only; no Atlassian credentials",
        )
        return metadata

    def command(self, worktree: Path, directory: Path, config: AgentConfig) -> list[str]:
        """Adapt the standard Codex argv to the read-only mounts and schema path."""
        base = super().command(worktree, directory, config)
        base[0] = "/agent/codex"
        base[base.index("--sandbox") + 1] = "read-only"
        base[base.index("--cd") + 1] = "/workspace"
        base[base.index("--output-last-message") + 1] = "/output/final-response.md"
        for index, arg in enumerate(base):
            if arg.startswith("projects.") and arg.endswith('.trust_level="untrusted"'):
                base[index] = f'projects.{json.dumps("/workspace")}.trust_level="untrusted"'
        base[-1:-1] = [
            "--skip-git-repo-check",
            "--output-schema",
            "/input/investigation-schema.json",
        ]
        return self.namespace(base, worktree, directory)
