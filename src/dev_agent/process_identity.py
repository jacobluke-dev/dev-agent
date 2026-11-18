"""Read Linux process identities and managed-session state for retry decisions."""

import os
from dataclasses import dataclass
from pathlib import Path

from dev_agent.records import RunRecord

PROC = Path("/proc")
TERMINAL = frozenset({"Z", "X", "x"})


@dataclass(frozen=True)
class ProcessState:
    """Identify one process in the current procfs view."""

    pid: int
    start_ticks: int
    session: int
    group: int
    state: str

    def identity(self) -> RunRecord:
        """Record the kernel identity without trusting the process name or command line."""
        return {"pid": self.pid, "start_ticks": self.start_ticks}


def process_state(pid: int) -> ProcessState | None:
    """Read one stat entry, distinguishing a disappeared process from an unreadable one."""
    if type(pid) is not int or pid < 1:
        raise ValueError("Invalid process ID")
    try:
        raw = (PROC / str(pid) / "stat").read_text(errors="replace")
    except FileNotFoundError, ProcessLookupError:
        return None
    fields = raw.rsplit(")", 1)[1].split()
    return ProcessState(pid, int(fields[19]), int(fields[3]), int(fields[2]), fields[0])


def process_scope() -> RunRecord:
    """Bind recorded PIDs to the same boot and matching procfs PID namespace."""
    if (PROC / "self").readlink().name != str(os.getpid()):
        raise ValueError("procfs does not expose the controller's PID namespace")
    return {
        "boot_id": (PROC / "sys/kernel/random/boot_id").read_text().strip(),
        "pid_namespace": str((PROC / "self/ns/pid").readlink()),
    }


def live_state(process: ProcessState) -> str:
    """Describe a nonterminal process for diagnostics."""
    return "stopped" if process.state in {"T", "t"} else "active"


def session_members(session: int) -> list[ProcessState]:
    """Inspect a dedicated session, including children reparented after controller exit."""
    return [
        process
        for path in PROC.iterdir()
        if path.name.isascii()
        and path.name.isdigit()
        and (process := process_state(int(path.name))) is not None
        and process.session == session
    ]


def recover_session(evidence: RunRecord) -> RunRecord:
    """Check recorded controller and installer identities without sending signals."""

    def blocked(reason: str) -> RunRecord:
        """Report why this checkpoint cannot authorize a retry."""
        return {"decision": "blocked", "reason": reason}

    try:
        if evidence.get("scope") != process_scope():
            return blocked("Recorded setup belongs to a different boot or PID namespace")
        controller = evidence["controller"]
        saved = evidence.get("child")
        if not saved:
            return blocked("Setup launch was interrupted before the installer identity was saved")
        pid = saved["pid"]
        child_start = saved["start_ticks"]
        controller_start = controller["start_ticks"]
        if (
            type(pid) is not int
            or pid < 2
            or saved["session"] != pid
            or type(child_start) is not int
            or type(controller_start) is not int
            or not 0 < controller_start <= child_start
        ):
            return blocked("Recorded installer session identity is invalid")
        owner = process_state(controller["pid"])
        if (
            owner
            and owner.identity() == controller
            and owner.pid != os.getpid()
            and owner.state not in TERMINAL
        ):
            return blocked(f"Recorded controller PID {owner.pid} is {live_state(owner)}")
        leader = process_state(pid)
        if leader and leader.start_ticks < child_start:
            return blocked(f"Installer PID {pid} does not match the recorded start time")
        if leader and leader.start_ticks > child_start:
            # Linux keeps the session ID reserved while any session member exists.
            # A different process at the leader PID proves the earlier session is gone.
            return {"decision": "retry", "reason": f"Installer PID {pid} was reused"}
        if leader and leader.state not in TERMINAL:
            return blocked(f"Recorded installer PID {pid} is {live_state(leader)}")
        for _ in range(2):
            members = session_members(pid)
            for member in members:
                if member.state not in TERMINAL:
                    return blocked(
                        f"Installer session {pid} still has {live_state(member)} PID {member.pid}"
                    )
        return {
            "decision": "retry",
            "reason": f"Installer session {pid} has no live members",
        }
    except OSError, KeyError, TypeError, ValueError, IndexError:
        return blocked("Cannot inspect the recorded installer identity and session in procfs")
