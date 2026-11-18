"""Test process identity and liveness checks used when reconciling stale setup."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dev_agent import process_identity
from dev_agent.process_identity import ProcessState, process_scope, process_state, recover_session


@pytest.mark.parametrize(
    ("owner", "installer", "members", "decision", "reason"),
    [
        ("T", None, [], "blocked", "controller PID 100 is stopped"),
        ("S", None, [], "blocked", "controller PID 100 is active"),
        ("Z", "Z", ["Z"], "retry", "no live members"),
        (None, None, [], "retry", "no live members"),
        ("reused", "reused", ["S"], "retry", "was reused"),
        (None, "T", ["T"], "blocked", "installer PID 200 is stopped"),
        (None, None, ["S"], "blocked", "still has active PID"),
        (None, None, ["T"], "blocked", "still has stopped PID"),
    ],
)
def test_recovery_uses_kernel_process_identity(
    monkeypatch: pytest.MonkeyPatch,
    owner: str | None,
    installer: str | None,
    members: list[str],
    decision: str,
    reason: str,
) -> None:
    """Distinguish terminated, live, stopped and unrelated PID occupants."""
    scope = {"boot_id": "fixture", "pid_namespace": "pid:[1]"}
    saved = {
        "scope": scope,
        "controller": {"pid": 100, "start_ticks": 10},
        "child": {"pid": 200, "session": 200, "start_ticks": 20},
    }
    processes = {
        pid: ProcessState(
            pid,
            ticks + (100 if state == "reused" else 0),
            200,
            200,
            "S" if state == "reused" else state,
        )
        for pid, state, ticks in [(100, owner, 10), (200, installer, 20)]
        if state
    }
    monkeypatch.setattr(process_identity, "process_scope", lambda: scope)
    monkeypatch.setattr(process_identity, "process_state", processes.get)
    monkeypatch.setattr(process_identity.os, "getpid", lambda: 999)
    monkeypatch.setattr(
        process_identity,
        "session_members",
        lambda _: [
            ProcessState(201 + i, 30, 200, 201 + i, state) for i, state in enumerate(members)
        ],
    )
    evidence = recover_session(saved)
    assert evidence["decision"] == decision
    assert reason.lower() in evidence["reason"].lower()


def test_unreadable_or_foreign_procfs_cannot_clear_a_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep foreign and incompletely observable installer sessions blocked."""
    scope = {"boot_id": "fixture", "pid_namespace": "pid:[1]"}
    saved = {
        "scope": scope,
        "controller": {"pid": 100, "start_ticks": 10},
        "child": {"pid": 200, "session": 200, "start_ticks": 20},
    }
    monkeypatch.setattr(
        process_identity, "process_scope", lambda: scope | {"pid_namespace": "pid:[2]"}
    )
    assert recover_session(saved)["decision"] == "blocked"
    monkeypatch.setattr(process_identity, "process_scope", lambda: scope)
    monkeypatch.setattr(process_identity, "process_state", lambda pid: None)

    def unreadable(session: int) -> list[ProcessState]:
        """Simulate procfs refusing a descendant status read."""
        raise PermissionError

    monkeypatch.setattr(process_identity, "session_members", unreadable)
    assert recover_session(saved)["decision"] == "blocked"


def test_reparented_installer_descendant_blocks_retry(tmp_path: Path) -> None:
    """Find a remaining descendant by session even after its leader has exited."""
    spawn = (
        "import sys,subprocess,time; from pathlib import Path; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1]], process_group=0); "
        "time.sleep(.25)"
    )
    worker = (
        "import time; from pathlib import Path; "
        "end = time.monotonic() + 10\n"
        "while not Path('finish').exists() and time.monotonic() < end: time.sleep(.01)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", spawn, worker], cwd=tmp_path, start_new_session=True
    )
    try:
        leader = process_state(child.pid)
        controller = process_state(os.getpid())
        assert leader and controller
        receipt = {
            "scope": process_scope(),
            "controller": controller.identity(),
            "child": {**leader.identity(), "session": leader.session},
        }
        assert child.wait(timeout=5) == 0
        assert recover_session(receipt)["decision"] == "blocked"
        (tmp_path / "finish").touch()
        deadline = time.monotonic() + 5
        while recover_session(receipt)["decision"] != "retry":
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        (tmp_path / "finish").touch()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
