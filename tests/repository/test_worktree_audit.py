"""Protect the Git-aware audit budget and its ignored-path security evidence."""

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from dev_agent.config import RepositoryContext
from dev_agent.errors import DevAgentError
from dev_agent.repository import instructions
from dev_agent.repository.git import git, output, source_state
from dev_agent.repository.instructions import (
    InstructionError,
    approved_scopes,
    audit_files,
    check_documents,
    resolve,
    supplied,
    validate_changes,
)
from dev_agent.repository.snapshot import ContextError


@pytest.mark.parametrize("directory", [".venv", "node_modules"])
def test_large_git_ignored_dependencies_are_never_opened_for_content(
    repo: Path, directory: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignore sparse dependency bytes while retaining metadata and source fingerprints."""
    (repo / ".gitignore").write_text(f"{directory}/\n")
    state = source_state(repo)["fingerprint"]
    dependency = repo / directory / "pkg" / "sparse.bin"
    dependency.parent.mkdir(parents=True)
    with dependency.open("wb") as stream:
        stream.truncate(3 * 1024**3)

    original = instructions.audit_file_hash

    def no_dependency_read(fd: int, name: str, initial: os.stat_result) -> str:
        """Fail if the audit tries to hash the ignored sparse file."""
        assert name != "sparse.bin"
        return original(fd, name, initial)

    monkeypatch.setattr(instructions, "audit_file_hash", no_dependency_read)
    audit = audit_files(repo, RepositoryContext(max_audit_bytes=1024))
    assert "metadata_only" in audit[dependency.relative_to(repo).as_posix()]
    assert "sha256" not in audit[dependency.relative_to(repo).as_posix()]
    assert source_state(repo)["fingerprint"] == state
    assert source_state(repo)["dirty_including_ignored"]
    assert dependency.stat().st_size == 3 * 1024**3


def test_git_index_and_non_ignored_untracked_paths_are_hashed(repo: Path) -> None:
    """Include tracked source under ignored names and force-added entries in the index."""
    source = repo / ".venv" / "tracked.py"
    source.parent.mkdir()
    source.write_text("tracked baseline")
    git(repo, "add", ".venv/tracked.py")
    git(repo, "commit", "-m", "fixture tracked source with a dependency-like dirname")
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / "notes.txt").write_text("nonignored untracked file")
    source.write_text("modified tracked")
    forced = source.with_name("new.py")
    forced.write_text("force-added entry")
    before_stage = source_state(repo)["fingerprint"]
    git(repo, "add", "-f", ".venv/new.py")
    git(repo, "add", "-u", ".venv/tracked.py")
    assert source_state(repo)["fingerprint"] != before_stage

    audit = audit_files(repo, RepositoryContext(max_audit_bytes=1024))
    for path in (source, forced, repo / "notes.txt"):
        assert (
            audit[path.relative_to(repo).as_posix()]["sha256"]
            == hashlib.sha256(path.read_bytes()).hexdigest()
        )
    before_edit = source_state(repo)["fingerprint"]
    source.write_text("working-tree edit after staging")
    assert source_state(repo)["fingerprint"] != before_edit
    assert (
        audit_files(repo, RepositoryContext())[".venv/tracked.py"]["sha256"]
        != audit[".venv/tracked.py"]["sha256"]
    )


@pytest.mark.parametrize("eligible", ["untracked-dependency-directory", "force-added-ignored"])
def test_oversized_eligible_content_fails_closed(repo: Path, eligible: str) -> None:
    """Apply the same byte limit to unignored content and files explicitly added to Git."""
    path = repo / "node_modules" / "large.txt"
    path.parent.mkdir()
    path.write_bytes(b"x" * 1025)
    if eligible == "force-added-ignored":
        (repo / ".gitignore").write_text("node_modules/\n")
        git(repo, "add", "-f", "node_modules/large.txt")
    with pytest.raises(ContextError, match="max_audit_bytes"):
        audit_files(repo, RepositoryContext(max_audit_bytes=1024))


@pytest.mark.parametrize("outside", [False, True])
def test_source_parent_symlink_is_rejected(repo: Path, tmp_path: Path, outside: bool) -> None:
    """Refuse tracked paths redirected to ignored or external directories."""
    original = repo / "source"
    original.mkdir()
    (original / "owned.py").write_text("tracked source")
    git(repo, "add", "source/owned.py")
    shutil.rmtree(original)
    target = tmp_path / "outside" if outside else repo / "ignored"
    target.mkdir()
    (target / "owned.py").write_bytes(b"x" * 2048)
    original.symlink_to(target, target_is_directory=True)
    with pytest.raises(DevAgentError, match="through a symlink"):
        source_state(repo)
    with pytest.raises(ContextError, match="crosses a symlink"):
        audit_files(repo, RepositoryContext(max_audit_bytes=1024))


def test_ignored_stat_snapshot_detects_out_of_scope_edits(repo: Path) -> None:
    """Keep detecting ignored changes with the same size and a restored modification time."""
    path = repo / "ignored" / "cache.bin"
    path.parent.mkdir()
    path.write_text("old")
    info = path.stat()
    policy = RepositoryContext()
    bundle = resolve(repo, output(repo, "rev-parse", "HEAD"), policy)
    state = bundle.provenance()
    supplied(bundle, state, bundle.render())
    before = audit_files(repo, policy)
    path.write_text("new")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    after = audit_files(repo, policy)
    result = validate_changes(
        bundle, state, before, after, approved_scopes(bundle, ("hello.py",)), repo
    )
    assert result["violations"] == [
        {"path": "ignored/cache.bin", "reason": "outside approved modification scope"}
    ]


def test_prepared_dependency_instructions_never_gain_authority(repo: Path) -> None:
    """Ignore an installed package's guidance while enforcing tracked guidance beside it."""
    (repo / ".gitignore").write_text("node_modules/\n")
    tracked = repo / "node_modules/owned/AGENTS.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("Maintain the owned package.")
    git(repo, "add", ".gitignore")
    git(repo, "add", "-f", "node_modules/owned/AGENTS.md")
    git(repo, "commit", "-m", "Track required package guidance")
    policy = RepositoryContext(max_audit_bytes=1024)
    bundle = resolve(repo, output(repo, "rev-parse", "HEAD"), policy)
    fingerprint = source_state(repo)["fingerprint"]
    dependency = repo / "node_modules/next/AGENTS.md"
    dependency.parent.mkdir()
    dependency.write_text("External package instructions")
    scopes = approved_scopes(bundle, (".",))
    files = audit_files(repo, policy)
    check_documents(bundle, files, scopes, worktree=repo)
    assert "metadata_only" in files["node_modules/next/AGENTS.md"]
    assert dependency.read_text() not in bundle.render()
    assert bundle.applicable("node_modules/next/index.js") == ["AGENTS.md"]
    assert bundle.applicable("node_modules/owned/index.js") == [
        "AGENTS.md",
        "node_modules/owned/AGENTS.md",
    ]
    assert source_state(repo)["fingerprint"] == fingerprint

    git(repo, "add", "-f", "node_modules/next/AGENTS.md")
    with pytest.raises(InstructionError, match="absent from the baseline manifest"):
        check_documents(bundle, audit_files(repo, policy), scopes, worktree=repo)
    tracked.write_text("Modified tracked instructions")
    with pytest.raises(InstructionError, match="Required baseline context changed"):
        check_documents(bundle, audit_files(repo, policy), scopes, worktree=repo)


def test_special_ignored_files_remain_unsafe(repo: Path) -> None:
    """Refuse special files without opening a FIFO, even in an ignored subtree."""
    directory = repo / "ignored"
    directory.mkdir()
    os.mkfifo(directory / "fifo")
    with pytest.raises(ContextError, match="special file"):
        audit_files(repo, RepositoryContext())


def test_older_content_audits_retain_bounded_comparison(repo: Path) -> None:
    """Rehash paths recorded in old evidence within the same byte budget."""
    path = repo / "ignored" / "first.bin"
    path.parent.mkdir()
    payload = b"x" * 2048
    path.write_bytes(payload)
    historical = {
        "ignored/first.bin": {
            "type": "file",
            "mode": path.stat().st_mode,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    }
    with pytest.raises(ContextError, match="max_audit_bytes"):
        audit_files(repo, RepositoryContext(max_audit_bytes=1024), previous=historical)
    current = audit_files(repo, RepositoryContext(max_audit_bytes=4096), previous=historical)
    assert current["ignored/first.bin"] == historical["ignored/first.bin"]
