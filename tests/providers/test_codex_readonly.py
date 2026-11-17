"""Check that Codex investigation uses the expected mounts and fails closed without isolation."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from dev_agent.errors import IsolationError
from dev_agent.providers.codex import CodexRunner
from dev_agent.providers.codex_readonly import CodexInvestigationRunner
from tests.support.atlassian import atlassian as atlassian


def test_read_only_command_and_namespace_mount_boundaries(tmp_path: Path) -> None:
    """Limit native Codex mounts to required runtime, credentials and run-specific paths."""
    runner = CodexInvestigationRunner()
    runner.executable = "/usr/bin/native-codex"
    runner.bwrap = "/usr/bin/bwrap"
    runner.auth = tmp_path / "auth.json"
    runner.auth.write_text("{}")
    repository = tmp_path / "repository"
    repository.mkdir()
    output_dir = tmp_path / "agent"
    output_dir.mkdir()
    from dev_agent.config import AgentConfig

    argv = runner.command(repository, output_dir, AgentConfig())
    assert argv[0] == "/usr/bin/bwrap"
    assert "--clearenv" in argv and "--unshare-pid" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv and "--output-schema" in argv
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv and "--add-dir" not in argv
    assert 'approval_policy="never"' in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert ["--ro-bind", str(repository), "/workspace"] == argv[
        argv.index(str(repository)) - 1 : argv.index(str(repository)) + 2
    ]
    assert ["--bind", str(output_dir), "/output"] == argv[
        argv.index(str(output_dir)) - 1 : argv.index(str(output_dir)) + 2
    ]
    assert "--proc" in argv and "--tmpfs" in argv
    assert "/etc/ssl/private" not in argv and "/etc/ssl" not in argv
    assert not any("DEV_AGENT_TEST_" in arg for arg in argv)


@pytest.mark.parametrize("case", ["no_bwrap", "no_auth", "namespace_denied"])
def test_read_only_preflight_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Refuse to run when the required Bubblewrap probe fails."""

    def base_preflight(self: CodexRunner) -> dict:
        """Resolve a synthetic executable while skipping real Codex CLI discovery."""
        self.executable = "/fake/codex"
        return {"provider": "codex"}

    monkeypatch.setattr(CodexRunner, "preflight", base_preflight)
    monkeypatch.setattr(
        "dev_agent.providers.codex_readonly.shutil.which",
        lambda *args, **kwargs: None if case == "no_bwrap" else "/fake/bwrap",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    if case != "no_auth":
        (tmp_path / "auth.json").write_text("{}")
    monkeypatch.setattr(
        "dev_agent.providers.codex_readonly.subprocess.run", Mock(return_value=Mock(returncode=1))
    )
    with pytest.raises(IsolationError):
        CodexInvestigationRunner().preflight()
