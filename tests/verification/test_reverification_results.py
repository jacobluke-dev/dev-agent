"""Protect verification profile selection, evidence history and source identity."""

import copy
import json
import os
import sys
from pathlib import Path

import pytest

from dev_agent.cli import main
from dev_agent.config import Config, load_config
from dev_agent.delivery.implementation import implement, plan
from dev_agent.errors import DevAgentError
from dev_agent.records import Store
from dev_agent.repository.git import create_worktree, git, output
from dev_agent.run_admin import inspect_run
from dev_agent.verification.runs import verify
from tests.support.runs import configured, python_check, runner


def profiles(config: Config) -> Config:
    """Configure one passing targeted and one failing full check for fixture runs."""
    return configured(
        config,
        verification_profiles={
            "targeted": (python_check("targeted-test", "print('targeted ran')"),),
            "full": (python_check("full-test", "import sys; print('full failed'); sys.exit(2)"),),
        },
    )


@pytest.mark.parametrize(
    "profile,expected", [("targeted", "passed"), ("full", "verification_failed"), (None, "passed")]
)
def test_verification_profiles(
    config: Config, prompt: Path, profile: str | None, expected: str
) -> None:
    """Run only the selected check profile and report its actual outcome."""
    config = profiles(config)
    record = implement(config, "example", "TASK-1", prompt, verification=profile, runner=runner())
    assert record["outcome"] == expected
    assert record["verification_profile"] == (profile or "targeted")
    assert record["verification"][0]["name"] == (
        "full-test" if profile == "full" else "targeted-test"
    )


def test_legacy_config_has_no_invented_profile(config: Config, prompt: Path) -> None:
    """Preserve legacy checks without silently inventing a full profile."""
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    assert record["verification_profile"] == "legacy"
    assert record["verification"][0]["name"] == "check"
    with pytest.raises(DevAgentError, match="not configured"):
        implement(config, "example", "TASK-1", prompt, verification="full", runner=runner())


def test_full_ci_profile_runs_in_its_worktree_and_records_failure(
    config_file: Path,
    repo: Path,
    prompt: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run full CI through a fake command and record its failure in the task worktree."""
    (repo / "Makefile").write_text("ci-local:\n\t@false\n")
    git(repo, "add", "Makefile")
    git(repo, "commit", "-m", "Add fixture CI target")
    config_file.write_text(
        config_file.read_text()
        + """
[[projects.example.verification_profiles.targeted]]
name = "unit"
argv = ["true"]
[[projects.example.verification_profiles.full]]
name = "ci-local"
argv = ["make", "ci-local"]
cwd = "."
timeout_seconds = 3600
required_paths = ["Makefile"]
[projects.example.verification_profiles.full.env]
UV_LOCKED = "1"
"""
    )
    tools = tmp_path / "test-tools"
    tools.mkdir()
    make = tools / "make"
    make.write_text(f"""#!{sys.executable}
import os
import sys
from pathlib import Path

assert sys.argv[1:] == ["ci-local"]
assert Path("Makefile").is_file()
assert os.environ["UV_LOCKED"] == "1"
assert "TEST_PRONUNCIATION_DSN" not in os.environ
Path("ignored").mkdir()
Path("ignored/ci-marker").touch()
print(Path.cwd())
print("Fixture CI stage failed", file=sys.stderr)
sys.exit(2)
""")
    make.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(tools), os.environ["PATH"])))
    monkeypatch.setenv("TEST_PRONUNCIATION_DSN", "discarded-fixture-environment")

    config = load_config(config_file)
    sibling = implement(config, "example", "TASK-2", prompt, runner=runner())
    original = implement(config, "example", "TASK-1", prompt, runner=runner())
    initial_attempt = copy.deepcopy(original["verification_attempts"][0])
    record = verify(config, original["run_id"], "full")

    targeted, full = record["verification_attempts"]
    assert targeted == initial_attempt
    assert targeted["profile"] == "targeted"
    assert targeted["outcome"] == record["outcome"] == "passed"
    assert full["outcome"] == "verification_failed"
    assert full["source_before"]["fingerprint"] == full["source_after"]["fingerprint"]
    assert full["configuration_sha256"] == config.source_digest
    check = full["checks"][0]
    assert check["required"] is True
    assert check["exit_code"] == 2
    assert Path(check["stdout_log"]).read_text().strip() == record["worktree"]
    assert Path(check["stderr_log"]).read_text().strip() == "Fixture CI stage failed"
    assert (Path(record["worktree"]) / "ignored/ci-marker").is_file()
    assert not (Path(sibling["worktree"]) / "ignored").exists()
    assert not (repo / "ignored").exists()


def test_reverification_history_preserves_original_evidence(
    config: Config, prompt: Path, fake_codex: Path
) -> None:
    """Append attempts without rewriting the original verification, logs or outcome."""
    config = profiles(config)
    record = implement(config, "example", "ISSUE-42", prompt, verification="targeted")
    original = copy.deepcopy(record)
    canonical = config.runs_root / record["run_id"]
    events = (canonical / "agent-events.jsonl").read_bytes()
    agent_output = (canonical / "final-response.md").read_bytes()
    first_attempt = record["verification_attempts"][0]
    export = Path(record["results_directory"])
    first_report = export / "verification" / first_attempt["attempt_id"] / "report.json"
    first_report_bytes = first_report.read_bytes()
    for _ in range(2):
        record = verify(config, record["run_id"], "full")
    assert record["agent"] == original["agent"]
    assert record["verification"] == original["verification"]
    assert record["outcome"] == original["outcome"] == "passed"
    assert len(record["verification_attempts"]) == 3
    assert record["verification_attempts"][0] == first_attempt
    assert len({a["attempt_id"] for a in record["verification_attempts"]}) == 3
    assert record["verification_attempts"][-1]["outcome"] == "verification_failed"
    assert record["verification_attempts"][-1]["changed_since_original"] is False
    assert first_report.read_bytes() == first_report_bytes
    assert (canonical / "agent-events.jsonl").read_bytes() == events
    assert (canonical / "final-response.md").read_bytes() == agent_output
    assert output(Path(record["worktree"]), "rev-parse", "HEAD") == original["baseline_commit"]
    text = (export / "summary.md").read_text()
    assert "Original outcome: `passed`" in text
    assert "Full verification: `failed`" in text
    assert "not been approved" in text


@pytest.mark.parametrize("change", ["content", "staged", "untracked", "commit", "mode"])
def test_changed_source_detection(config: Config, prompt: Path, change: str) -> None:
    """Detect source, mode or index changes before reverification."""
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    worktree = Path(record["worktree"])
    if change == "untracked":
        (worktree / "new.txt").write_text("new source")
    elif change == "mode":
        (worktree / "hello.py").chmod(0o755)
    else:
        (worktree / "hello.py").write_text("VALUE = 999\n")
        if change in ("staged", "commit"):
            git(worktree, "add", "hello.py")
        if change == "commit":
            git(worktree, "commit", "-m", "human change")
    record = verify(config, record["run_id"])
    latest = record["verification_attempts"][-1]
    assert latest["changed_since_original"] is True
    assert latest["source_before"]["head_commit"] == output(worktree, "rev-parse", "HEAD")
    assert latest["source_before"]["fingerprint"] != record["implementation_source"]["fingerprint"]


def test_ignored_environment_state_is_explicit(config: Config, prompt: Path) -> None:
    """Expose ignored-file dirtiness without including it in the verification fingerprint."""
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    worktree = Path(record["worktree"])
    (worktree / "ignored").mkdir()
    (worktree / "ignored/cache").write_text("cache")
    attempt = verify(config, record["run_id"])["verification_attempts"][-1]
    assert attempt["changed_since_original"] is False
    assert attempt["source_before"]["dirty_including_ignored"] is True
    assert "ignored files excluded" in attempt["source_before"]["scope"]


def test_checks_modifying_source_cannot_pass(config: Config, prompt: Path) -> None:
    """Reject a successful check exit if source changed during the attempt."""
    config = configured(
        config,
        verification=(
            python_check(
                "mutate", "from pathlib import Path; Path('hello.py').write_text('changed')"
            ),
        ),
    )
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    assert record["outcome"] == "verification_failed"
    attempt = record["verification_attempts"][0]
    assert attempt["checks"][0]["status"] == "passed"
    assert attempt["source_changed_during_verification"] is True
    assert attempt["status"] == "source_changed"


def test_reverification_does_not_prepare_without_explicit_flag(
    config: Config, prompt: Path
) -> None:
    """Run setup again only when reverification explicitly requests it."""
    config = configured(
        config,
        preparation=(
            python_check(
                "setup",
                "from pathlib import Path; p=Path('counter'); "
                "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
            ),
        ),
    )
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    worktree = Path(record["worktree"])
    assert (worktree / "counter").read_text() == "1"
    verify(config, record["run_id"])
    assert (worktree / "counter").read_text() == "1"
    later = verify(config, record["run_id"], prepare_environment=True)
    assert (worktree / "counter").read_text() == "2"
    attempt = later["verification_attempts"][-1]
    assert attempt["preparation"]["status"] == "passed"
    assert attempt["changed_since_original"] is True
    assert attempt["source_before"]["fingerprint"] != attempt["source_requested"]["fingerprint"]


def test_missing_verification_prerequisite_reports_evidence(config: Config, prompt: Path) -> None:
    """Record missing prerequisites as unavailable verification before running checks."""
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    config = configured(
        config,
        verification=(python_check("missing", "print('never')", required_paths=("absent",)),),
    )
    attempt = verify(config, record["run_id"])["verification_attempts"][-1]
    assert attempt["outcome"] == "environment_failed"
    assert attempt["status"] == "missing_prerequisite"
    assert attempt["checks"][0]["status"] == "not_run"
    assert "Missing required worktree path: absent" in attempt["prerequisites"]["failures"][0]


@pytest.mark.parametrize("invalid", ["missing", "branch", "locked", "cleaned"])
def test_reverification_refuses_invalid_worktree(
    config: Config, prompt: Path, tmp_path: Path, invalid: str
) -> None:
    """Reject invalid, cleaned or locked retained execution state before reverification."""
    from dev_agent.run_admin import cleanup

    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    worktree = Path(record["worktree"])
    store = Store(config.runs_root)
    if invalid == "missing":
        worktree.rename(tmp_path / "moved")
    elif invalid == "branch":
        git(worktree, "checkout", "--detach")
    elif invalid == "cleaned":
        cleanup(config, record["run_id"], confirmed=True)
    if invalid == "locked":
        with store.lock(record["run_id"]):
            with pytest.raises(DevAgentError, match="active"):
                verify(config, record["run_id"])
    else:
        with pytest.raises(DevAgentError):
            verify(config, record["run_id"])
    assert len(store.load(record["run_id"])["verification_attempts"]) == 1


def test_historical_run_compatibility(config: Config, prompt: Path) -> None:
    """Read and reverify a compatible legacy record without rewriting its original outcome."""
    record, _ = plan(config, "example", "TASK-OLD", prompt)
    record.update(
        schema_version=1,
        outcome="verification_failed",
        phase="finished",
        implementation_status="agent_completed",
        verification_status="failed",
    )
    record["agent"].update(status="passed", exit_code=0)
    for name in ("verification_attempts", "preparation", "prerequisites", "verification_profile"):
        record.pop(name, None)
    record["verification"][0]["status"] = "failed"
    store = Store(config.runs_root)
    canonical = store.create(record)
    create_worktree(
        config.project("example"),
        record["branch"],
        Path(record["worktree"]),
        record["baseline_commit"],
    )
    (canonical / "agent-events.jsonl").write_text('{"type":"original-evidence"}\n')
    (canonical / "final-response.md").write_text("Historical implementation")
    original = copy.deepcopy(record)
    assert inspect_run(config, record["run_id"])["preparation_status"] == "not_recorded"
    assert not config.results_root.exists()
    later = verify(config, record["run_id"])
    assert later["schema_version"] == 1
    assert later["outcome"] == "verification_failed"
    assert later["agent"] == original["agent"]
    assert later["verification"] == original["verification"]
    assert later["verification_attempts"][0]["changed_since_original"] is None
    assert "unknown" in later["verification_attempts"][0]["comparison_note"]
    assert (canonical / "agent-events.jsonl").read_text() == '{"type":"original-evidence"}\n'
    assert store.directory(record["run_id"]) == canonical
    assert (
        Path(later["results_directory"]) / "agent-output.md"
    ).read_text() == "Historical implementation"


def test_cli_profiles_results_inspection_and_arbitrary_cwd(
    config_file: Path,
    prompt: Path,
    fake_codex: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Resolve explicit configuration and profile commands from outside the repository."""
    config_file.write_text(
        config_file.read_text()
        + """
[[projects.example.verification_profiles.targeted]]
name="unit"
argv=["true"]
[[projects.example.verification_profiles.full]]
name="regression"
argv=["false"]
"""
    )
    monkeypatch.chdir(tmp_path)
    prefix = ["--config", str(config_file)]
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
                "--verification",
                "targeted",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(prefix + ["runs", "list", "--json"]) == 0
    run_id = json.loads(capsys.readouterr().out)[0]["run_id"]
    assert main(prefix + ["results", run_id]) == 0
    assert Path(capsys.readouterr().out.strip()).is_dir()
    assert main(prefix + ["verify", run_id, "--profile", "full", "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["outcome"] == "verification_failed"
    assert main(prefix + ["inspect", run_id, "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["outcome"] == "passed"
    assert info["latest_verification"]["outcome"] == "verification_failed"
    assert info["preparation_status"] == "skipped"
    assert info["results_directory"]


def test_reverification_export_failure_preserves_original_outcome(
    config: Config, prompt: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist the attempt and export error without replacing the original successful outcome."""
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    original = copy.deepcopy(record)

    def failed_export(*args: object) -> None:
        """Raise a controlled results export failure after canonical evidence is saved."""
        raise OSError("export unavailable")

    monkeypatch.setattr("dev_agent.reporting.results.export_results", failed_export)
    latest = verify(config, record["run_id"])
    assert latest["outcome"] == original["outcome"] == "passed"
    assert latest["agent"] == original["agent"]
    assert latest["verification"] == original["verification"]
    assert latest["verification_attempts"][-1]["outcome"] == "passed"
    assert "export unavailable" in latest["results_error"]
    assert Store(config.runs_root).load(record["run_id"])["results_error"]


def test_environment_failure_exit_code(
    config_file: Path, prompt: Path, fake_codex: Path, capsys: pytest.CaptureFixture
) -> None:
    """Return the documented environment-failure exit status when prerequisites fail."""
    config_file.write_text(
        config_file.read_text()
        + """
[[projects.example.preparation]]
name="required-setup"
argv=["false"]
"""
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
        == 6
    )
    output = capsys.readouterr()
    assert "[FAIL] environment failed" in output.out
    assert "report.json" in output.out and "check-required-setup.stderr.log" in output.out
