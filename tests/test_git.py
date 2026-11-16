from dataclasses import replace
from pathlib import Path

import pytest

from dev_agent.config import Config
from dev_agent.errors import DevAgentError
from dev_agent.git import (
    check_destination,
    create_worktree,
    git,
    output,
    repository_instructions,
    status,
    validate_repository,
    worktrees,
)


def test_repository_baseline(config: Config, repo: Path) -> None:
    baseline = validate_repository(config.project("example"), config.runs_root)
    assert baseline.commit == output(repo, "rev-parse", "refs/heads/main")
    assert baseline.common_dir == repo / ".git"
    assert not baseline.source_dirty


def test_worktree_preserves_dirty_source_and_exact_base(config: Config, repo: Path) -> None:
    project = config.project("example")
    commit = output(repo, "rev-parse", "main")
    git(repo, "switch", "-c", "existing")
    (repo / "hello.py").write_text("source dirty\n")
    (repo / "untracked.txt").write_text("keep\n")
    (repo / "ignored").mkdir()
    (repo / "ignored/secret.txt").write_text("keep ignored\n")
    original = status(repo, ignored=True)
    baseline = validate_repository(project, config.runs_root)
    destination = project.worktree_root / "test"
    create_worktree(project, "dev-agent/test", destination, baseline.commit)
    assert baseline.source_dirty
    assert output(destination, "rev-parse", "HEAD") == commit
    assert (destination / "hello.py").read_text() == "VALUE = 1\n"
    assert not (destination / "untracked.txt").exists()
    assert output(repo, "branch", "--show-current") == "existing"
    assert status(repo, ignored=True) == original
    assert (repo / "ignored/secret.txt").read_text() == "keep ignored\n"
    assert worktrees(repo)[destination]["branch"] == "refs/heads/dev-agent/test"


@pytest.mark.parametrize("kind", ["branch", "directory", "file", "symlink", "registered_missing"])
def test_collisions_refused(config: Config, tmp_path: Path, kind: str) -> None:
    project = config.project("example")
    project.worktree_root.mkdir()
    destination = project.worktree_root / "collision"
    if kind == "branch":
        git(project.repository, "branch", "dev-agent/collision")
    elif kind == "directory":
        destination.mkdir()
    elif kind == "file":
        destination.write_text("preserve")
    elif kind == "symlink":
        destination.symlink_to(tmp_path / "nonexistent", target_is_directory=True)
    else:
        create_worktree(
            project, "dev-agent/other", destination, output(project.repository, "rev-parse", "HEAD")
        )
        destination.rename(tmp_path / "manually-moved")
    with pytest.raises(DevAgentError, match="already exists|registered"):
        check_destination(project, "dev-agent/collision", destination)


@pytest.mark.parametrize(
    "case",
    ["missing", "subdirectory", "unborn", "bare", "missing_branch", "invalid_branch", "filter"],
)
def test_repository_validation_failures(config: Config, tmp_path: Path, case: str) -> None:
    project = config.project("example")
    if case == "missing":
        project = replace(project, repository=tmp_path / "missing")
    elif case == "subdirectory":
        sub = project.repository / "sub"
        sub.mkdir()
        project = replace(project, repository=sub)
    elif case in ("unborn", "bare"):
        other = tmp_path / case
        other.mkdir()
        git(other, "init", *(["--bare"] if case == "bare" else ["-b", "main"]))
        project = replace(project, repository=other)
    elif case == "missing_branch":
        project = replace(project, base_branch="missing")
    elif case == "invalid_branch":
        project = replace(project, base_branch="../bad")
    else:
        git(project.repository, "config", "filter.hostile.smudge", "false")
    with pytest.raises(DevAgentError):
        validate_repository(project, config.runs_root)


def test_committed_instructions_do_not_follow_symlinks(
    config: Config, repo: Path, tmp_path: Path
) -> None:
    external = tmp_path / "secret"
    external.write_text("never read this")
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(external)
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-m", "symlink instruction")
    with pytest.raises(DevAgentError, match="regular"):
        repository_instructions(repo, output(repo, "rev-parse", "HEAD"))


def test_included_checkout_filter_rejected(config: Config, tmp_path: Path) -> None:
    project = config.project("example")
    included = tmp_path / "included.gitconfig"
    included.write_text('[filter "hostile"]\nsmudge = echo should-not-run\n')
    git(project.repository, "config", "include.path", str(included))
    with pytest.raises(DevAgentError, match="checkout filters"):
        validate_repository(project, config.runs_root)


def test_checkout_hooks_and_fsmonitor_not_executed(config: Config, tmp_path: Path) -> None:
    project = config.project("example")
    marker = tmp_path / "must-not-exist"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    script = hooks / "post-checkout"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    script.chmod(0o755)
    git(project.repository, "config", "core.hooksPath", str(hooks))
    git(project.repository, "config", "core.fsmonitor", str(script))
    baseline = validate_repository(project, config.runs_root)
    create_worktree(
        project, "dev-agent/hooks-test", project.worktree_root / "hooks-test", baseline.commit
    )
    assert not marker.exists()
