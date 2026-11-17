"""Run subprocesses with bounded lifetimes and private output files.

Providers and trusted verification commands share this execution machinery. The default
environment is an allowlist. The caller owns additional environment policy and any filesystem
sandboxing.
"""

import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


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


def stop(process: subprocess.Popen[bytes]) -> None:
    """Signal the subprocess group, escalate after a short wait and reap the child."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Kill surviving descendants even if their parent has already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def execute(
    argv: list[str],
    *,
    cwd: Path,
    stdout: Path,
    stderr: Path,
    timeout: int,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    """Run an argument vector in a dedicated process group and capture its output.

    The caller supplies an existing working directory and two new log paths. Logs are created
    exclusively with owner-only permissions. Optional stdin is encoded as UTF-8. An explicit
    environment replaces the default allowlist.

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
            process.communicate(input=stdin.encode("utf-8") if stdin else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(process)
            return ProcessResult(process.returncode, time.monotonic() - started, timed_out=True)
        except KeyboardInterrupt as exc:
            stop(process)
            raise ProcessInterrupted(
                ProcessResult(process.returncode, time.monotonic() - started, error="Interrupted")
            ) from exc
        except BaseException:
            stop(process)
            raise
    return ProcessResult(process.returncode, time.monotonic() - started)
