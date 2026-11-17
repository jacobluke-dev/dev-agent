"""Protect results exports and canonical evidence when writes fail."""

import json
from pathlib import Path

import pytest

from dev_agent.config import Config
from dev_agent.delivery.implementation import implement
from dev_agent.errors import DevAgentError
from dev_agent.records import Store, atomic_json
from dev_agent.reporting.results import export_results
from tests.support.runs import runner


def test_results_layout_duplicate_tasks_and_summary(
    config: Config, prompt: Path, fake_codex: Path
) -> None:
    """Retain distinct private exports and readable evidence for repeated task IDs."""
    records = [implement(config, "example", "TASK-123", prompt) for _ in range(2)]
    assert records[0]["run_id"] != records[1]["run_id"]
    for record in records:
        directory = config.results_root / "TASK-123" / record["run_id"]
        assert Path(record["results_directory"]) == directory
        assert all(
            (directory / name).exists()
            for name in (
                "summary.md",
                "report.json",
                "agent-events.jsonl",
                "agent-output.md",
                "preparation",
                "verification",
            )
        )
        report = json.loads((directory / "report.json").read_text())
        assert report["run_id"] == record["run_id"]
        summary = (directory / "summary.md").read_text()
        assert "# TASK-123 — Execution Results" in summary
        assert "hello.py" in summary and "Implemented VALUE change" in summary
        assert "[stdout](verification/" in summary
        assert "No failures recorded" in summary
        assert "not been approved" in summary
        assert "did not commit, push, merge or deploy" in summary
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / "report.json").stat().st_mode & 0o777 == 0o600
        assert "turn.completed" not in summary


@pytest.mark.parametrize(
    "task", ["../TASK-123", "a/b", "a\\b", "..", "-bad", "name with spaces", "λ", "/absolute"]
)
def test_unsafe_task_no_results(config: Config, prompt: Path, task: str) -> None:
    """Reject unsafe task identifiers before opening a results destination."""
    with pytest.raises(DevAgentError):
        implement(config, "example", task, prompt, runner=runner())
    assert not config.results_root.exists()
    assert not config.runs_root.exists()


def test_existing_results_cannot_be_overwritten(
    config: Config, prompt: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse to replace a preexisting directory owned by another execution."""
    directory = config.results_root / "TASK-1" / "existing-run"
    directory.mkdir(parents=True)
    (directory / "summary.md").write_text("precious evidence")
    monkeypatch.setattr("dev_agent.delivery.implementation.new_id", lambda *args: "existing-run")
    with pytest.raises(DevAgentError, match="already exists"):
        implement(config, "example", "TASK-1", prompt, runner=runner())
    assert (directory / "summary.md").read_text() == "precious evidence"
    assert not config.project("example").worktree_root.exists()


def test_results_symlink_escape_refused(config: Config, prompt: Path, tmp_path: Path) -> None:
    """Reject a results path that resolves through a symlink."""
    config.results_root.mkdir()
    external = tmp_path / "external-results"
    external.mkdir()
    (config.results_root / "TASK-1").symlink_to(external, target_is_directory=True)
    with pytest.raises(DevAgentError, match="symlinks"):
        implement(config, "example", "TASK-1", prompt, runner=runner())
    assert list(external.iterdir()) == []


def test_atomic_report_failure_keeps_previous_export(
    config: Config, prompt: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve the last exported report after a failed replace operation."""
    import os

    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    report = Path(record["results_directory"]) / "report.json"
    previous = report.read_bytes()
    original_replace = os.replace

    def fail_report(source: Path, target: Path) -> None:
        """Fail only the final report replacement while allowing other fixture writes."""
        if target == report:
            raise OSError("simulated export storage failure")
        original_replace(source, target)

    monkeypatch.setattr("dev_agent.records.os.replace", fail_report)
    with pytest.raises(OSError, match="simulated"):
        export_results(config, record)
    assert report.read_bytes() == previous
    assert not list(report.parent.glob(".write-*.tmp"))
    assert Store(config.runs_root).load(record["run_id"])["outcome"] == "passed"


def test_atomic_canonical_failure_keeps_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the previous canonical record when atomic replacement fails."""
    target = tmp_path / "record.json"
    atomic_json(target, {"old": True})
    original = target.read_bytes()
    monkeypatch.setattr(
        "dev_agent.records.os.replace",
        lambda *args: (_ for _ in ()).throw(OSError("storage failed")),
    )
    with pytest.raises(OSError):
        atomic_json(target, {"new": True})
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".write-*.tmp"))
