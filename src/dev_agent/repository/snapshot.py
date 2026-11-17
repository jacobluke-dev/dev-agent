"""Build and inspect sealed source snapshots for static analysis.

Materialisation reads immutable Git objects at an exact commit. It does not run checkout filters
or follow symlinks. Investigation records the resulting manifest and integrity fingerprint.
"""

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from dev_agent.config import Project, relative_path
from dev_agent.errors import DevAgentError
from dev_agent.records import RunRecord
from dev_agent.repository.git import git


class ContextError(DevAgentError):
    """Carry a context failure and the document evidence captured before it."""

    def __init__(self, message: str, documents: list[RunRecord]):
        """Preserve the message and available source documents for diagnostics."""
        super().__init__(message)
        self.documents = documents


def excluded(path: str) -> bool:
    """Identify control directories and dotenv paths omitted from sealed snapshots."""
    return any(
        part in {".git", ".codex", ".agents", ".aws"} or part == ".env" or part.startswith(".env.")
        for part in Path(path).parts
    )


def tree_entries(repository: Path, commit: str) -> list[RunRecord]:
    """Decode Git tree entries while rejecting unsafe or unsupported file names."""
    entries = []
    for item in git(repository, "ls-tree", "-r", "-l", "-z", commit).stdout.split(b"\x00"):
        if not item:
            continue
        meta, raw_path = item.split(b"\t", 1)
        mode, kind, oid, size = meta.split()
        try:
            path = raw_path.decode("utf-8")
            relative_path(path, "Git snapshot path")
        except UnicodeError, DevAgentError:
            raise DevAgentError(
                "Baseline contains an unsupported or unsafe snapshot filename"
            ) from None
        entries.append(
            {
                "path": path,
                "mode": mode.decode(),
                "type": kind.decode(),
                "oid": oid.decode(),
                "size": int(size) if size != b"-" else 0,
            }
        )
    return entries


def materialize(
    project: Project,
    commit: str,
    directory: Path,
    sanitize: Callable[[str], str] = lambda value: value,
) -> tuple[list[RunRecord], list[str]]:
    """Copy bounded regular Git blobs into a new read-only directory.

    Return a manifest and warnings for omitted entries. The source is an exact commit, not the
    active worktree. The optional sanitiser acts as a guard against known credentials. Oversized
    or inconsistent snapshots fail instead of exposing partial context.
    """
    entries = tree_entries(project.repository, commit)
    accepted = []
    warnings = []
    total = 0
    for entry in entries:
        if excluded(entry["path"]) or entry["mode"] not in ("100644", "100755"):
            entry["status"] = "omitted"
            entry["reason"] = "credential/config path, symlink or submodule; never materialized"
            warnings.append(f"Snapshot omitted {entry['path']}: {entry['reason']}")
        else:
            accepted.append(entry)
            total += entry["size"]
    if (
        len(entries) > project.investigation.max_snapshot_files
        or total > project.investigation.max_snapshot_bytes
    ):
        raise DevAgentError(
            "Baseline exceeds investigation snapshot limits; no partial code snapshot accepted"
        )
    directory.mkdir(mode=0o700)
    # Batch immutable object IDs; filenames never become Git revision expressions.
    raw = (
        git(
            project.repository,
            "cat-file",
            "--batch",
            input="".join(f"{e['oid']}\n" for e in accepted).encode(),
        ).stdout
        if accepted
        else b""
    )
    offset = 0
    for entry in accepted:
        end = raw.index(b"\n", offset)
        oid, kind, size = raw[offset:end].split()
        count = int(size)
        content = raw[end + 1 : end + 1 + count]
        if (
            oid.decode() != entry["oid"]
            or kind != b"blob"
            or count != entry["size"]
            or len(content) != count
        ):
            raise DevAgentError("Git blob snapshot response failed integrity checks")
        offset = end + 1 + count + 1
        target = directory / entry["path"]
        text = content.decode("utf-8", errors="replace")
        if sanitize(text) != text or sanitize(entry["path"]) != entry["path"]:
            raise DevAgentError(
                "Configured credential values occur in baseline files; refusing to expose them"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        target.chmod(0o444)
        entry.update(
            status="materialized",
            content_sha256=hashlib.sha256(content).hexdigest(),
            lines=len(content.splitlines()),
        )
    for child in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir():
            child.chmod(0o555)
    directory.chmod(0o555)
    return entries, warnings


def snapshot_fingerprint(directory: Path) -> str:
    """Hash the snapshot's paths, modes and file contents while rejecting substitutions."""
    digest = hashlib.sha256()
    if directory.is_symlink() or directory.resolve() != directory:
        raise DevAgentError("Investigation snapshot path contains a symlink")
    for path in sorted(directory.rglob("*")):
        digest.update(str(path.relative_to(directory)).encode() + b"\x00")
        digest.update(str(path.lstat().st_mode).encode() + b"\x00")
        if path.is_symlink():
            raise DevAgentError("Investigation snapshot acquired a symlink")
        if path.is_file():
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
        elif not path.is_dir():
            raise DevAgentError("Investigation snapshot acquired a special file")
    digest.update(str(directory.stat().st_mode).encode())
    return digest.hexdigest()


def remove_snapshot(directory: Path) -> None:
    """Remove the caller-validated snapshot without accepting a symlink at its root.

    The caller owns confirmation and integrity policy. Directory permissions are widened inside
    the snapshot so sealed contents can be removed.
    """
    # Caller must validate exact canonical run path and confirm cleanup first.
    if directory.is_symlink() or directory.resolve() != directory:
        raise DevAgentError("Refusing cleanup through a snapshot symlink")
    for parent, dirs, _ in os.walk(directory, followlinks=False):
        Path(parent).chmod(0o700)
        for name in dirs:
            child = Path(parent) / name
            if not child.is_symlink():
                child.chmod(0o700)
    shutil.rmtree(directory)
