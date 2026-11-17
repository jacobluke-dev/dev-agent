"""Test subprocess argument handling, environment filtering and process-group shutdown."""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from dev_agent.process import ProcessInterrupted, environment, execute


def test_shell_metacharacters_are_literal(tmp_path: Path) -> None:
    """Pass metacharacters as literal arguments without launching a shell."""
    out, err = tmp_path / "out", tmp_path / "err"
    result = execute(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "$(touch marker); &"],
        cwd=tmp_path,
        stdout=out,
        stderr=err,
        timeout=5,
    )
    assert result.ok
    assert out.read_text() == "$(touch marker); &\n"
    assert not (tmp_path / "marker").exists()


def test_sensitive_environment_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exclude unrelated credentials and configuration overrides from the default environment."""
    for key in (
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GIT_DIR",
        "PYTHONPATH",
        "HTTP_PROXY",
        "CODEX_CONFIG",
    ):
        monkeypatch.setenv(key, "sensitive")
        assert key not in environment()
    monkeypatch.setenv("CODEX_HOME", "/auth/location")
    assert environment()["CODEX_HOME"] == "/auth/location"


def test_timeout_kills_descendants(tmp_path: Path) -> None:
    """Stop the timed-out subprocess group including its child processes."""
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, time, pathlib; "
        f"p=subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    result = execute(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        stdout=tmp_path / "out",
        stderr=tmp_path / "err",
        timeout=1,
    )
    assert result.timed_out
    pid = int(pidfile.read_text())
    proc_status = Path(f"/proc/{pid}/status")
    if proc_status.exists():
        # A killed child may briefly remain a zombie until the host reaps it.
        assert "State:\tZ" in proc_status.read_text()
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_process_interrupt_captures_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Propagate a stopped child's exit evidence after an interruption."""
    child = Mock()
    child.communicate.side_effect = KeyboardInterrupt
    child.returncode = -15
    monkeypatch.setattr("dev_agent.process.subprocess.Popen", Mock(return_value=child))
    stopped = Mock()
    monkeypatch.setattr("dev_agent.process.stop", stopped)
    with pytest.raises(ProcessInterrupted) as error:
        execute(["fake"], cwd=tmp_path, stdout=tmp_path / "out", stderr=tmp_path / "err", timeout=5)
    stopped.assert_called_once_with(child)
    assert error.value.result.exit_code == -15
    assert error.value.result.duration_seconds >= 0
