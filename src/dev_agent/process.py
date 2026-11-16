"""Subprocess execution with a small inherited environment and process-group timeouts."""

import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


def environment() -> dict[str, str]:
    # Do not inherit API keys, cloud credentials, Git overrides or Python startup hooks.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "CODEX_HOME"}
    return {key: value for key, value in os.environ.items() if key in allowed}


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None

    def record(self) -> dict[str, int | float | bool | str | None]:
        return asdict(self)


class ProcessInterrupted(KeyboardInterrupt):
    def __init__(self, result: ProcessResult) -> None:
        super().__init__("Subprocess interrupted")
        self.result = result


def stop(process: subprocess.Popen[bytes]) -> None:
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
) -> ProcessResult:
    started = time.monotonic()
    with stdout.open("xb") as out, stderr.open("xb") as err:
        os.fchmod(out.fileno(), 0o600)
        os.fchmod(err.fileno(), 0o600)
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment(),
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        except OSError as exc:
            return ProcessResult(None, time.monotonic() - started, error=str(exc))
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
