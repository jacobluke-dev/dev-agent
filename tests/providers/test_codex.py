"""Check the workspace-write Codex adapter's CLI, safety and completion contract."""

import json
import subprocess
from pathlib import Path

import pytest

from dev_agent.config import AgentConfig
from dev_agent.errors import DevAgentError
from dev_agent.process import ProcessResult
from dev_agent.providers.codex import CodexRunner


def test_unsupported_codex_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a CLI that does not advertise all required execution safeguards."""
    monkeypatch.setattr("dev_agent.providers.codex.shutil.which", lambda *a, **kw: "/fake/codex")
    monkeypatch.setattr(
        "dev_agent.providers.codex.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, "old", ""),
    )
    with pytest.raises(DevAgentError, match="required safety"):
        CodexRunner().preflight()


def test_codex_command_safety_and_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass prompts via stdin and apply restrained workspace, approval and network options."""
    runner = CodexRunner()
    runner.executable = "/fake/codex"
    worktree = tmp_path / "work tree"
    worktree.mkdir()
    directory = tmp_path / "run"
    directory.mkdir()
    seen = {}

    def fake_execute(argv: list[str], **kwargs: object) -> ProcessResult:
        """Capture runner arguments and supply minimal successful completion artifacts."""
        seen.update(argv=argv, **kwargs)
        (directory / "agent-events.jsonl").write_text('{"type":"turn.completed"}\n')
        (directory / "final-response.md").write_text("done")
        return ProcessResult(0, 0.1)

    monkeypatch.setattr("dev_agent.providers.codex.execute", fake_execute)
    assert runner.run(
        worktree, "private prompt", directory, AgentConfig("test-model", "high", 7)
    ).ok
    argv = seen["argv"]
    assert argv[:2] == ["/fake/codex", "exec"]
    assert argv[-1] == "-"
    assert seen["stdin"] == "private prompt"
    assert seen["cwd"] == worktree
    assert seen["timeout"] == 7
    assert "private prompt" not in argv
    assert "--add-dir" not in argv
    assert not any("bypass" in arg or "danger-full-access" in arg for arg in argv)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    overrides = dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "-c")
    assert overrides["approval_policy"] == '"never"'
    assert overrides["sandbox_workspace_write.network_access"] == "false"
    assert overrides["sandbox_workspace_write.writable_roots"] == "[]"
    assert overrides["features.hooks"] == "false"
    assert overrides["web_search"] == '"disabled"'
    assert overrides["model_reasoning_effort"] == '"high"'
    assert overrides[f"projects.{json.dumps(str(worktree))}.trust_level"] == '"untrusted"'


@pytest.mark.parametrize(
    "events,final",
    [
        ('{"type":"turn.failed"}\n', "done"),
        ('{"type":"turn.completed"}\n{"type":"error"}\n', "done"),
        ('{"type":"thread.started"}\n', "done"),
        ("not json\n", "done"),
        ("[]\n", "done"),
        ('{"type":"turn.completed"}\n', ""),
    ],
)
def test_zero_exit_without_completion_evidence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: str, final: str
) -> None:
    """Reject zero exits with incomplete, malformed or contradictory completion artifacts."""
    runner = CodexRunner()
    runner.executable = "/fake/codex"

    def fake_execute(*args: object, **kwargs: object) -> ProcessResult:
        """Write the fixture event stream and final response for completion validation."""
        (tmp_path / "agent-events.jsonl").write_text(events)
        (tmp_path / "final-response.md").write_text(final)
        return ProcessResult(0, 0.1)

    monkeypatch.setattr("dev_agent.providers.codex.execute", fake_execute)
    result = runner.run(tmp_path, "prompt", tmp_path, AgentConfig())
    assert not result.ok
    assert result.exit_code == 0
    assert result.error
