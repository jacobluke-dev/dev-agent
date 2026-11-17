"""The unqualified MCP migration must not enable an unsafe or imaginary provider."""

from pathlib import Path

import pytest

from dev_agent.config import AgentConfig, load_config
from dev_agent.errors import DevAgentError
from dev_agent.providers.codex import CodexRunner
from dev_agent.providers.codex_readonly import CodexInvestigationRunner


@pytest.mark.parametrize(
    "configuration",
    [
        '[projects.example.mcp]\nenabled = true\nservers = ["atlassian"]\nread_only = true',
        '[projects.example.mcp]\nenabled = true\nservers = ["atlassian", "unrelated"]',
        "[projects.example.mcp]\nenabled = false",
    ],
)
def test_unqualified_project_mcp_settings_are_not_silently_accepted(
    config_file: Path, configuration: str
) -> None:
    """Refuse configuration that implies an available project MCP switch."""
    config_file.write_text(config_file.read_text() + "\n" + configuration + "\n")
    with pytest.raises(DevAgentError, match="Unknown example settings: mcp"):
        load_config(config_file)


def test_mcp_cannot_be_selected_as_arbitrary_agent_configuration(config_file: Path) -> None:
    """Reject MCP server definitions embedded in agent settings."""
    config_file.write_text(
        config_file.read_text().replace('model = "fake-model"', 'mcp_servers = ["atlassian"]')
    )
    with pytest.raises(DevAgentError, match="Unknown agent settings: mcp_servers"):
        load_config(config_file)


@pytest.mark.parametrize("investigation", [False, True])
def test_production_runners_do_not_inherit_user_mcp_or_plugin_configuration(
    tmp_path: Path,
    investigation: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep user MCP and plugin configuration out of both restricted runner modes."""
    home = tmp_path / "synthetic-codex-home"
    home.mkdir()
    config = home / "config.toml"
    config.write_text("""[mcp_servers.unrelated]
command = "must-not-execute"
[plugins."unrelated@example"]
enabled = true
""")
    auth = home / "auth.json"
    auth.write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))
    runner = CodexInvestigationRunner() if investigation else CodexRunner()
    runner.executable = "/fake/codex"
    if investigation:
        runner.bwrap = "/fake/bwrap"
        runner.auth = auth
    worktree = tmp_path / "worktree"
    directory = tmp_path / "run"
    argv = runner.command(worktree, directory, AgentConfig())
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert argv[argv.index("--sandbox") + 1] == (
        "read-only" if investigation else "workspace-write"
    )
    assert 'approval_policy="never"' in argv
    assert "features.plugins=false" in argv and "features.apps=false" in argv
    assert "project_doc_max_bytes=0" in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert "--trust" not in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert not any("mcp_servers" in part or "must-not-execute" in part for part in argv)
    assert str(config) not in argv
    if investigation:
        assert str(home) not in argv  # Only the existing login file is bound, never the home.
        assert str(auth) in argv
    assert config.read_text().startswith("[mcp_servers.unrelated]")
