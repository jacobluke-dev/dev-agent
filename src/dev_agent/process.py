"""Run subprocesses with bounded lifetimes and private output files.

Providers and trusted verification commands share this execution machinery. The default
environment is an allowlist. The caller owns additional environment policy and any filesystem
sandboxing.
"""

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

STOP_GRACE_SECONDS = 2
CLEANUP_ERROR = (
    "Subprocess cleanup could not be confirmed; inspect the process group before retrying"
)


def environment() -> dict[str, str]:
    """Copy the small default subprocess environment from the current process."""
    # Do not inherit API keys, cloud credentials, Git overrides or Python startup hooks.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "CODEX_HOME"}
    return {key: value for key, value in os.environ.items() if key in allowed}


@dataclass(frozen=True)
class ProcessResult:
    """Hold exit, timing, timeout and launch evidence for one subprocess."""

    exit_code: int | None
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None
    failure_kind: str | None = None

    @property
    def ok(self) -> bool:
        """Report whether the process exited zero without a timeout or recorded error."""
        return self.exit_code == 0 and not self.timed_out and self.error is None

    def record(self) -> dict[str, int | float | bool | str | None]:
        """Convert the result into fields suitable for a canonical run record."""
        return asdict(self)


class ProcessInterrupted(KeyboardInterrupt):
    """Carry the stopped child's process evidence through a KeyboardInterrupt."""

    def __init__(self, result: ProcessResult) -> None:
        """Attach the evidence captured after the interrupted child was stopped."""
        super().__init__("Subprocess interrupted")
        self.result = result


def stop(process: subprocess.Popen[bytes]) -> bool:
    """Signal the group and bound the wait for the direct child, even if the group is gone."""
    signalled = True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        # Check for surviving descendants even if the first wait reaped their leader.
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            signalled = False
        try:
            process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                return False
    return signalled


def execute(
    argv: list[str],
    *,
    cwd: Path,
    stdout: Path,
    stderr: Path,
    timeout: int,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    on_started: Callable[[int], None] | None = None,
) -> ProcessResult:
    """Run an argument vector in a dedicated process group and capture its output.

    The caller supplies an existing working directory and two new log paths. Logs are created
    exclusively with owner-only permissions. Optional stdin is encoded as UTF-8. An explicit
    environment replaces the default allowlist. The optional callback checkpoints the
    child identity before any wait and participates in subprocess cleanup on failure.

    Returns:
        Exit and duration evidence, including launch failures and timeouts.

    Raises:
        ProcessInterrupted: If interrupted while waiting for the child. The group is stopped
            first.
        OSError: If the output files cannot be opened or secured.
    """
    started = time.monotonic()
    with stdout.open("xb") as out, stderr.open("xb") as err:
        os.fchmod(out.fileno(), 0o600)
        os.fchmod(err.fileno(), 0o600)
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment() if env is None else env,
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        except OSError as exc:
            return ProcessResult(
                None,
                time.monotonic() - started,
                error=str(exc),
                failure_kind="missing_launch_prerequisite"
                if isinstance(exc, FileNotFoundError)
                else "launch_error",
            )
        try:
            if on_started:
                on_started(process.pid)
            if stdin:
                process.communicate(input=stdin.encode("utf-8"), timeout=timeout)
            else:
                process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            reaped = stop(process)
            return ProcessResult(
                process.returncode,
                time.monotonic() - started,
                timed_out=True,
                error=None if reaped else CLEANUP_ERROR,
                failure_kind=None if reaped else "cleanup_incomplete",
            )
        except KeyboardInterrupt as exc:
            reaped = stop(process)
            raise ProcessInterrupted(
                ProcessResult(
                    process.returncode,
                    time.monotonic() - started,
                    error="Interrupted" if reaped else CLEANUP_ERROR,
                    failure_kind=None if reaped else "cleanup_incomplete",
                )
            ) from exc
        except BaseException:
            stop(process)
            raise
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
    return ProcessResult(process.returncode, time.monotonic() - started)
