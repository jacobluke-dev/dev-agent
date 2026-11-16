"""Private JSON records with atomic replacement and per-run advisory locks (Linux)."""

import fcntl
import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dev_agent.errors import DevAgentError

RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
type RunRecord = dict[str, Any]


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(project: str, task: str) -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{project}-{task}-{uuid.uuid4().hex[:8]}"


class Store:
    def __init__(self, root: Path):
        self.root = root

    def directory(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise DevAgentError("Invalid run ID")
        candidate = self.root / run_id
        if candidate.is_symlink() or candidate.resolve().parent != self.root.resolve():
            raise DevAgentError("Run directory must not be a symlink or leave runs_root")
        return candidate

    def create(self, record: RunRecord) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.directory(record["run_id"])
        directory.mkdir(mode=0o700)
        self.save(record)
        return directory

    def save(self, record: RunRecord) -> None:
        directory = self.directory(record["run_id"])
        temporary = directory / f".record-{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, indent=2, ensure_ascii=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "record.json")
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, run_id: str) -> RunRecord:
        filename = self.directory(run_id) / "record.json"
        if filename.is_symlink():
            raise DevAgentError("Run record must not be a symlink")
        try:
            record = json.loads(filename.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DevAgentError(f"Cannot read run {run_id}: {exc}") from exc
        if (
            not isinstance(record, dict)
            or record.get("run_id") != run_id
            or record.get("schema_version") != 1
        ):
            raise DevAgentError(f"Invalid run record: {run_id}")
        return record

    def list(self) -> list[RunRecord]:
        if not self.root.exists():
            return []
        return [
            self.load(p.name)
            for p in sorted(self.root.iterdir(), reverse=True)
            if p.is_dir() and not p.is_symlink() and RUN_ID.fullmatch(p.name)
        ]

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        filename = self.directory(run_id) / ".lock"
        fd = os.open(filename, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DevAgentError(
                    "Run is active or being cleaned up; try again after it stops"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def log(directory: Path, message: str) -> None:
    filename = directory / "run.log"
    fd = os.open(filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(f"{now()} {message}\n")
