"""Exercise CLI dispatch, run inspection and explicitly authorised local cleanup across packages."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from dev_agent.cli import main
from dev_agent.config import Config
from dev_agent.delivery.implementation import implement
from dev_agent.errors import DevAgentError
from dev_agent.process import ProcessResult
from dev_agent.records import Store
from dev_agent.repository.git import git, output, status
from dev_agent.run_admin import cleanup, inspect_run


def clean_run(config: Config, prompt: Path) -> dict:
    """Create a temporary implementation run with a fake successful agent."""
    runner = Mock()
    runner.preflight.return_value = {"provider": "codex", "version": "fake"}
    runner.run.return_value = ProcessResult(0, 0.1)
    return implement(config, "example", "TASK-1", prompt, runner=runner)


def test_cleanup_requires_confirmation_and_retains_branch(
    config: Config, prompt: Path, repo: Path
) -> None:
    """Require confirmation before removal and retain the branch and canonical metadata."""
    record = clean_run(config, prompt)
    worktree = Path(record["worktree"])
    original = status(repo)
    with pytest.raises(DevAgentError, match="confirmation"):
        cleanup(config, record["run_id"], confirmed=False)
    assert worktree.exists()
    result = cleanup(config, record["run_id"], confirmed=True)
    assert not worktree.exists()
    assert result["cleanup"]["branch_retained"]
    assert output(repo, "rev-parse", record["branch"]) == record["baseline_commit"]
    assert status(repo) == original
    assert output(repo, "branch", "--show-current") == "main"
    assert Store(config.runs_root).load(record["run_id"])["cleanup"]
    assert cleanup(config, record["run_id"], confirmed=True) == result
    assert inspect_run(config, record["run_id"])["cleanup"]


@pytest.mark.parametrize("kind", ["tracked", "untracked", "ignored", "staged", "nested_repo"])
def test_cleanup_preserves_uncommitted_work(config: Config, prompt: Path, kind: str) -> None:
    """Protect modified, untracked and ignored files during ordinary cleanup."""
    record = clean_run(config, prompt)
    worktree = Path(record["worktree"])
    if kind in ("tracked", "staged"):
        filename = worktree / "hello.py"
        filename.write_text("uncommitted changes\n")
        if kind == "staged":
            git(worktree, "add", "hello.py")
    elif kind == "ignored":
        (worktree / "ignored").mkdir()
        filename = worktree / "ignored/precious.txt"
        filename.write_text("precious\n")
    elif kind == "nested_repo":
        nested = worktree / "nested"
        nested.mkdir()
        git(nested, "init", "-b", "main")
        filename = nested / "precious.txt"
        filename.write_text("precious\n")
    else:
        filename = worktree / "precious.txt"
        filename.write_text("precious\n")
    original = filename.read_text()
    with pytest.raises(DevAgentError, match="uncommitted/untracked/ignored"):
        cleanup(config, record["run_id"], confirmed=True)
    assert filename.read_text() == original


def test_force_still_requires_confirmation(config: Config, prompt: Path) -> None:
    """Require affirmative consent even when the force flag is supplied."""
    record = clean_run(config, prompt)
    worktree = Path(record["worktree"])
    (worktree / "hello.py").write_text("dirty\n")
    with pytest.raises(DevAgentError, match="confirmation"):
        cleanup(config, record["run_id"], confirmed=False, force=True)
    assert worktree.exists()
    result = cleanup(config, record["run_id"], confirmed=True, force=True)
    assert result["cleanup"]["forced"]
    assert not worktree.exists()


def test_cleanup_refuses_active_run(config: Config, prompt: Path) -> None:
    """Prevent cleanup while another operation holds the run lock."""
    record = clean_run(config, prompt)
    with Store(config.runs_root).lock(record["run_id"]):
        with pytest.raises(DevAgentError, match="active"):
            cleanup(config, record["run_id"], confirmed=True)
    assert Path(record["worktree"]).exists()


@pytest.mark.parametrize(
    "case", ["config", "report_path", "branch", "detached", "symlink", "gitfile", "locked"]
)
def test_cleanup_refuses_identity_changes(
    config: Config, prompt: Path, case: str, tmp_path: Path
) -> None:
    """Refuse to remove a worktree whose registered identity has changed."""
    record = clean_run(config, prompt)
    worktree = Path(record["worktree"])
    if case == "config":
        config = replace(
            config,
            projects={
                "example": replace(config.project("example"), worktree_root=tmp_path / "other")
            },
        )
    elif case == "report_path":
        record["worktree"] = str(config.project("example").repository)
        Store(config.runs_root).save(record)
    elif case == "branch":
        git(worktree, "switch", "-c", "manual-branch")
    elif case == "detached":
        git(worktree, "checkout", "--detach")
    elif case == "symlink":
        moved = tmp_path / "moved"
        worktree.rename(moved)
        worktree.symlink_to(moved, target_is_directory=True)
    elif case == "gitfile":
        (worktree / ".git").write_text(f"gitdir: {config.project('example').repository / '.git'}\n")
    elif case == "locked":
        git(config.project("example").repository, "worktree", "lock", str(worktree))
    with pytest.raises(DevAgentError):
        cleanup(config, record["run_id"], confirmed=True, force=True)
    assert worktree.exists()


def test_committed_changes_retained_after_cleanup(config: Config, prompt: Path, repo: Path) -> None:
    """Keep committed task changes reachable through the branch after worktree removal."""
    record = clean_run(config, prompt)
    worktree = Path(record["worktree"])
    (worktree / "hello.py").write_text("committed change\n")
    git(worktree, "add", "hello.py")
    git(worktree, "commit", "-m", "human-approved commit")
    sha = output(worktree, "rev-parse", "HEAD")
    assert inspect_run(config, record["run_id"])["files_changed"] == ["hello.py"]
    cleanup(config, record["run_id"], confirmed=True)
    assert output(repo, "rev-parse", record["branch"]) == sha
    assert output(repo, "rev-parse", "main") == record["baseline_commit"]


def test_cli_workflow(
    config_file: Path, prompt: Path, fake_codex: Path, capsys: pytest.CaptureFixture
) -> None:
    """Exercise the implementation, listing, inspection and cleanup command sequence."""
    prefix = ["--config", str(config_file)]
    assert main(prefix + ["projects", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "example"
    assert (
        main(
            prefix
            + [
                "implement",
                "TASK-1",
                "--project",
                "example",
                "--prompt-file",
                str(prompt),
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert main(prefix + ["runs", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert (
        main(prefix + ["implement", "TASK-1", "--project", "example", "--prompt-file", str(prompt)])
        == 0
    )
    capsys.readouterr()
    assert main(prefix + ["runs", "list"]) == 0
    run_id = json.loads(capsys.readouterr().out)[0]["run_id"]
    assert main(prefix + ["runs", "show", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "passed"
    assert main(prefix + ["inspect", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["dirty_including_ignored"]
    assert main(prefix + ["cleanup", run_id, "--yes"]) == 2
    assert "uncommitted" in capsys.readouterr().err
    assert main(prefix + ["cleanup", run_id, "--force", "--yes"]) == 0
    assert "permanently lost" in capsys.readouterr().out


@pytest.mark.parametrize(
    "outcome,code",
    [
        ("succeeded", 0),
        ("agent_failed", 3),
        ("verification_failed", 4),
        ("unverified", 4),
        ("orchestration_failed", 5),
        ("interrupted", 130),
    ],
)
def test_cli_exit_codes(
    config_file: Path,
    prompt: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    outcome: str,
    code: int,
) -> None:
    """Map implementation outcomes to the documented command-line exit codes."""
    monkeypatch.setattr(
        "dev_agent.cli.implement",
        lambda *a, **kw: {"run_id": "run-1", "worktree": "/synthetic/tree", "outcome": outcome},
    )
    assert (
        main(
            [
                "--config",
                str(config_file),
                "implement",
                "TASK-1",
                "--project",
                "example",
                "--prompt-file",
                str(prompt),
            ]
        )
        == code
    )


def test_cli_errors(config_file: Path, prompt: Path, capsys: pytest.CaptureFixture) -> None:
    """Report malformed inputs and missing runs as local command errors."""
    assert (
        main(
            [
                "--config",
                str(config_file),
                "implement",
                "../bad",
                "--project",
                "example",
                "--prompt-file",
                str(prompt),
            ]
        )
        == 2
    )
    assert "Invalid task" in capsys.readouterr().err
    assert main(["--config", str(config_file), "runs", "show", "missing"]) == 2
    assert "Cannot read" in capsys.readouterr().err


def test_diff_inspection_does_not_execute_helpers(
    config: Config, prompt: Path, tmp_path: Path
) -> None:
    """Inspect a worktree without executing configured diff helpers."""
    record = clean_run(config, prompt)
    worktree = Path(record["worktree"])
    marker = tmp_path / "diff-helper-must-not-run"
    helper = tmp_path / "diff-helper"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat \"$1\"\n")
    helper.chmod(0o755)
    git(worktree, "config", "diff.external", str(helper))
    git(worktree, "config", "diff.custom.textconv", str(helper))
    (worktree / ".gitattributes").write_text("*.py diff=custom\n")
    (worktree / "hello.py").write_text("changed\n")
    info = inspect_run(config, record["run_id"])
    assert "hello.py" in info["files_changed"]
    assert not marker.exists()
