"""Own canonical run metadata and its private on-disk storage.

Workflows persist compatible versioned JSON here, and reporting reads it to produce exports.
File replacement is atomic and Linux advisory locks coordinate operations for one run.
"""

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
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def new_id(project: str, task: str) -> str:
    """Build a sortable run ID with project, task and a random collision suffix."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{project}-{task}-{uuid.uuid4().hex[:8]}"


class Store:
    """Locate and persist records under one operator-configured runs root.

    The store owns run-ID path validation, schema compatibility and per-run exclusion. Record
    creation and locking are separate operations.
    """

    def __init__(self, root: Path):
        """Bind record operations to the configured runs root."""
        self.root = root

    def directory(self, run_id: str) -> Path:
        """Resolve a run ID's expected path after rejecting unsafe IDs and symlinks."""
        if not RUN_ID.fullmatch(run_id):
            raise DevAgentError("Invalid run ID")
        candidate = self.root / run_id
        if candidate.is_symlink() or candidate.resolve().parent != self.root.resolve():
            raise DevAgentError("Run directory must not be a symlink or leave runs_root")
        return candidate

    def create(self, record: RunRecord) -> Path:
        """Reserve an owner-only run directory and write its first canonical record."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.directory(record["run_id"])
        directory.mkdir(mode=0o700)
        self.save(record)
        return directory

    def save(self, record: RunRecord) -> None:
        """Atomically replace the canonical JSON for a record in its reserved directory."""
        directory = self.directory(record["run_id"])
        atomic_json(directory / "record.json", record)

    def load(self, run_id: str) -> RunRecord:
        """Read a run and check its ID and supported schema.

        Raises:
            DevAgentError: If the record is absent, unreadable, symlinked or incompatible.
        """
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
            or record.get("schema_version") not in (1, 2)
        ):
            raise DevAgentError(f"Invalid run record: {run_id}")
        return record

    def list(self) -> list[RunRecord]:
        """Load canonical records for valid run directories in reverse ID order."""
        if not self.root.exists():
            return []
        return [
            self.load(p.name)
            for p in sorted(self.root.iterdir(), reverse=True)
            if p.is_dir() and not p.is_symlink() and RUN_ID.fullmatch(p.name)
        ]

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        """Acquire a nonblocking exclusive advisory lock for an existing run.

        The context releases the lock on exit. A different process holding it results in an
        actionable refusal.
        """
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
    """Append a timestamped line without following the run-log symlink."""
    filename = directory / "run.log"
    fd = os.open(filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(f"{now()} {message}\n")


def atomic_json(filename: Path, data: object) -> None:
    """Encode JSON as UTF-8 and replace the target through the atomic writer."""
    atomic_write(filename, (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode())


def atomic_write(filename: Path, data: bytes) -> None:
    """Write to a private sibling file, flush it and replace the destination atomically."""
    temporary = filename.parent / f".write-{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, filename)
    finally:
        temporary.unlink(missing_ok=True)
