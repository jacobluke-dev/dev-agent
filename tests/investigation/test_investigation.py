"""Exercise standalone investigation, retained evidence and sealed snapshot integrity with fakes."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from dev_agent.config import Config, InvestigationConfig, RepositoryContext
from dev_agent.errors import DevAgentError, IsolationError
from dev_agent.integrations.atlassian import RetrievalError
from dev_agent.investigation.workflow import investigate
from dev_agent.process import ProcessResult
from dev_agent.records import Store
from dev_agent.repository.git import git, output, status, worktrees
from dev_agent.run_admin import cleanup, inspect_run
from dev_agent.verification.runs import verify
from tests.support.atlassian import atlassian as atlassian
from tests.support.investigation import FakeInvestigator, investigation_config
from tests.support.investigation import investigator as investigator


def test_end_to_end_persistence_results_and_inspection(
    config: Config, atlassian: tuple, investigator: FakeInvestigator
) -> None:
    """Persist requirements, advisory findings and results without creating a task worktree."""
    provider, http = atlassian
    config = investigation_config(config, provider)
    record = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    assert record["outcome"] == "investigated"
    assert record["agent"]["exit_code"] == 0 and record["agent"]["sandbox"] == "read-only"
    assert record["suitability"] == "requires_human_decision"
    assert record["branch"] is None and record["worktree"] is None
    assert not config.project("example").worktree_root.exists()
    canonical = Store(config.runs_root).directory(record["run_id"])
    assert Store(config.runs_root).load(record["run_id"]) == record
    directory = Path(record["results_directory"])
    for name in record["expected_artifacts"]:
        assert (directory / name).is_file(), name
    snapshot = json.loads((directory / "requirements-snapshot.json").read_text())
    result = json.loads((directory / "investigation.json").read_text())
    assert len(result["criteria"]) == len(snapshot["requirements"])
    assert result["executed_verification"] == []
    assert record["source_changed"] is False and record["snapshot_changed"] is False
    assert inspect_run(config, record["run_id"])["snapshot_intact"] is True
    markdown = (directory / "investigation.md").read_text()
    for heading in (
        "Executive summary",
        "Sources examined",
        "Baseline",
        "Requirements assessment",
        "Existing implementation",
        "Missing or incomplete functionality",
        "Architectural constraints",
        "Testing assessment",
        "Dependencies and risks",
        "Autonomous delivery suitability",
        "Recommended next action",
    ):
        assert f"## {heading}" in markdown
    assert "hello.py:L1-L1" in markdown and record["baseline_commit"] in markdown
    assert "runtime unverified" in markdown and "not checked" in markdown
    assert (canonical / "repository/hello.py").read_text() == "VALUE = 1\n"
    assert all(call.get_method() == "GET" for call in http.calls)


def test_dirty_checkout_different_branch_baseline_isolation(
    config: Config, repo: Path, atlassian: tuple, investigator: FakeInvestigator
) -> None:
    """Assess the configured baseline independently of dirty files on the active branch."""
    provider, _ = atlassian
    baseline = output(repo, "rev-parse", "main")
    git(repo, "branch", "maintained")
    git(repo, "switch", "-c", "unrelated-feature")
    (repo / "hello.py").write_text("uncommitted feature\n")
    (repo / "private-local.txt").write_text("not baseline\n")
    config = investigation_config(config, provider)
    config = replace(
        config, projects={"example": replace(config.project("example"), base_branch="maintained")}
    )
    before = status(repo, ignored=True), output(repo, "show-ref"), worktrees(repo)
    record = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    assert record["outcome"] == "investigated"
    assert record["baseline_commit"] == baseline and record["base_branch"] == "maintained"
    assert record["source_dirty_at_start"]
    assert output(repo, "branch", "--show-current") == "unrelated-feature"
    assert before == (status(repo, ignored=True), output(repo, "show-ref"), worktrees(repo))
    assert not (Path(record["snapshot_directory"]) / "private-local.txt").exists()
    assert record["other_branches_inspected"] == [] and record["remote_freshness"] == "not_checked"


def test_dry_run_no_external_or_agent_or_storage(
    config: Config, repo: Path, atlassian: tuple
) -> None:
    """Preview investigation without HTTP, agent execution or allocated run storage."""
    provider, http = atlassian
    config = investigation_config(config, provider)
    agent = Mock()
    before = output(repo, "show-ref"), status(repo), worktrees(repo)
    record = investigate(
        config, "example", "TASK-123", provider=provider, runner=agent, dry_run=True
    )
    assert record["phase"] == "planned" and record["context"][0]["content_sha256"]
    assert "content" not in record["context"][0]
    assert record["provider"]["site_url"] == "https://example.atlassian.net"
    assert record["expected_artifacts"] and record["planned_results_directory"]
    assert not http.calls and not config.runs_root.exists() and not config.results_root.exists()
    agent.preflight.assert_not_called()
    agent.run.assert_not_called()
    assert before == (output(repo, "show-ref"), status(repo), worktrees(repo))


@pytest.mark.parametrize("case", ["missing_config", "invalid_key", "bad_baseline"])
def test_local_errors_before_external(config: Config, atlassian: tuple, case: str) -> None:
    """Reject invalid local configuration or task inputs before external retrieval."""
    provider, http = atlassian
    config = investigation_config(config, provider)
    task = "TASK-123"
    if case == "missing_config":
        config = replace(
            config, projects={"example": replace(config.project("example"), atlassian=None)}
        )
    elif case == "invalid_key":
        task = "../TASK-123"
    else:
        config = replace(
            config, projects={"example": replace(config.project("example"), base_branch="missing")}
        )
    with pytest.raises(DevAgentError):
        investigate(config, "example", task, dry_run=True)
    assert not http.calls and not config.runs_root.exists()


def test_required_retrieval_failure_stops_agent(
    config: Config, atlassian: tuple, investigator: FakeInvestigator
) -> None:
    """Keep failed required Jira retrieval from launching an investigation runner."""
    provider, _ = atlassian
    provider.retrieve = Mock(
        side_effect=RetrievalError("jira: authentication/permission failure (HTTP 401)")
    )
    record = investigate(
        investigation_config(config, provider),
        "example",
        "TASK-123",
        provider=provider,
        runner=investigator,
    )
    assert record["outcome"] == "retrieval_failed" and investigator.called == 0
    assert "401" in record["error"]
    assert not (
        Store(config.runs_root).directory(record["run_id"]) / "requirements-snapshot.json"
    ).exists()
    assert (Path(record["results_directory"]) / "investigation.md").is_file()


@pytest.mark.parametrize("optional", [False, True])
def test_required_optional_context_missing(
    config: Config, atlassian: tuple, investigator: FakeInvestigator, optional: bool
) -> None:
    """Fail on missing required context while recording missing optional context."""
    provider, _ = atlassian
    config = investigation_config(config, provider)
    ctx = RepositoryContext(
        instructions=("AGENTS.md",) if optional else ("absent.md",),
        optional_instructions=("absent.md",) if optional else (),
    )
    config = replace(config, projects={"example": replace(config.project("example"), context=ctx)})
    record = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    assert record["outcome"] == ("investigated" if optional else "context_failed")
    assert investigator.called == (1 if optional else 0)
    sources = json.loads((Path(record["results_directory"]) / "sources.json").read_text())
    assert any(
        doc["status"] == "unavailable" and doc["path"] == "absent.md" for doc in sources["context"]
    )


def test_context_limits_snapshot_limits_and_prompt_limits(
    config: Config, atlassian: tuple, investigator: FakeInvestigator
) -> None:
    """Bound context, snapshot and prompt sizes before launching the agent."""
    provider, _ = atlassian
    config = investigation_config(config, provider)
    for settings in (
        {"context": RepositoryContext(instructions=("AGENTS.md",), max_document_bytes=1)},
        {"investigation": InvestigationConfig(max_snapshot_files=1)},
        {"investigation": InvestigationConfig(max_prompt_bytes=1024)},
    ):
        candidate = replace(
            config, projects={"example": replace(config.project("example"), **settings)}
        )
        record = investigate(
            candidate, "example", "TASK-123", provider=provider, runner=investigator
        )
        assert record["outcome"] == "context_failed"
    assert investigator.called == 0


def test_snapshot_never_follows_symlinks_or_uses_export_ignore(
    config: Config, repo: Path, tmp_path: Path, atlassian: tuple, investigator: FakeInvestigator
) -> None:
    """Build the snapshot from safe Git blobs independently of archive attributes."""
    provider, _ = atlassian
    secret = tmp_path / "external"
    secret.write_text("not exported")
    (repo / "escape").symlink_to(secret)
    (repo / ".env").write_text("private settings")
    (repo / ".gitattributes").write_text("hello.py export-ignore\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "snapshot cases")
    record = investigate(
        investigation_config(config, provider),
        "example",
        "TASK-123",
        provider=provider,
        runner=investigator,
    )
    assert record["outcome"] == "investigated"
    root = Path(record["snapshot_directory"])
    assert not (root / "escape").exists() and not (root / ".env").exists()
    assert (root / "hello.py").is_file()
    assert any("omitted" in warning for warning in record["limitations"])
    assert secret.read_text() == "not exported"


@pytest.mark.parametrize("mutation", ["snapshot", "source"])
def test_detected_mutation_is_security_failure(
    config: Config, repo: Path, atlassian: tuple, investigator: FakeInvestigator, mutation: str
) -> None:
    """Reject accepted assessment output if source or sealed snapshot integrity changes."""
    provider, _ = atlassian
    config = investigation_config(config, provider)

    def mutate(root: Path) -> None:
        """Modify the selected fixture file to simulate an integrity violation."""
        path = (root if mutation == "snapshot" else repo) / "hello.py"
        path.chmod(0o644)
        path.write_text("VALUE = 2\n")

    investigator.mutate = mutate
    record = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    assert record["outcome"] == "security_failed"
    assert record[f"{mutation}_changed"] is True
    assert not (Path(record["results_directory"]) / "investigation.json").exists()
    assert "assessment rejected" in record["error"]


@pytest.mark.parametrize(
    "defect",
    [
        "missing_field",
        "missing_criterion",
        "changed_requirement",
        "bad_quote",
        "bad_path",
        "bad_range",
        "wrong_task",
        "wrong_baseline",
        "runtime_claim",
        "test_only_satisfied",
        "bad_risk_factors",
        "wrong_action",
        "malformed_json",
        "duplicate_json",
        "unsupported_status",
    ],
)
def test_invalid_structured_output_preserves_raw(
    config: Config, atlassian: tuple, investigator: FakeInvestigator, defect: str
) -> None:
    """Retain invalid raw responses without promoting them to accepted findings."""
    provider, _ = atlassian
    config = investigation_config(config, provider)

    def invalid(value: dict) -> object:
        """Inject the selected shape, requirement, citation or suitability defect."""
        if defect == "missing_field":
            del value["executive_summary"]
        elif defect == "missing_criterion":
            value["criteria"].pop()
        elif defect == "changed_requirement":
            value["criteria"][0]["requirement"] = "invented"
        elif defect == "bad_quote":
            value["criteria"][0]["evidence"][0]["quote"] = "absent evidence"
        elif defect == "bad_path":
            value["criteria"][0]["evidence"][0]["path"] = "../outside"
        elif defect == "bad_range":
            value["criteria"][0]["evidence"][0]["line_end"] = 99999
        elif defect == "wrong_task":
            value["task_id"] = "OTHER-1"
        elif defect == "wrong_baseline":
            value["baseline_commit"] = "0" * 40
        elif defect == "runtime_claim":
            value["criteria"][0]["runtime_verified"] = True
        elif defect == "test_only_satisfied":
            value["criteria"][0]["status"] = "satisfied"
            value["criteria"][0]["evidence"][0]["kind"] = "test_coverage"
        elif defect == "bad_risk_factors":
            value["delivery_factors"][-1] = value["delivery_factors"][0]
        elif defect == "wrong_action":
            value["recommended_next_action"] = "No implementation needed."
        elif defect == "malformed_json":
            return "not structured JSON"
        elif defect == "duplicate_json":
            return '{"schema_version":1,"schema_version":1}'
        elif defect == "unsupported_status":
            value["criteria"][0]["status"] = "probably"
        return value

    investigator.output = invalid
    record = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    assert record["outcome"] == "invalid_investigation"
    directory = Path(record["results_directory"])
    assert (directory / "agent-output.md").stat().st_size
    assert not (directory / "investigation.json").exists()
    assert record["agent"]["exit_code"] == 0


def test_agent_failure_and_isolation_failure(
    config: Config, atlassian: tuple, investigator: FakeInvestigator
) -> None:
    """Distinguish sandbox preflight failure from a failed agent execution."""
    provider, _ = atlassian
    config = investigation_config(config, provider)
    investigator.exit_code = 7
    record = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    assert record["outcome"] == "agent_failed" and record["agent"]["exit_code"] == 7
    investigator.preflight = Mock(side_effect=IsolationError("Bubblewrap unavailable"))
    investigator.called = 0
    record = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    assert record["outcome"] == "environment_failed" and investigator.called == 0
    assert (Path(record["results_directory"]) / "requirements-snapshot.json").is_file()


def test_repeated_investigations_cleanup_and_verify_refusal(
    config: Config, atlassian: tuple, investigator: FakeInvestigator
) -> None:
    """Keep separate investigations and refuse to run verification on their records."""
    provider, _ = atlassian
    config = investigation_config(config, provider)
    first = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    original = (Path(first["results_directory"]) / "report.json").read_bytes()
    second = investigate(config, "example", "TASK-123", provider=provider, runner=investigator)
    assert first["run_id"] != second["run_id"]
    assert (Path(first["results_directory"]) / "report.json").read_bytes() == original
    with pytest.raises(DevAgentError, match="cannot be implementation-verified"):
        verify(config, first["run_id"])
    with pytest.raises(DevAgentError, match="confirmation"):
        cleanup(config, first["run_id"], confirmed=False)
    with Store(config.runs_root).lock(first["run_id"]):
        with pytest.raises(DevAgentError, match="active"):
            cleanup(config, first["run_id"], confirmed=True)
    cleaned = cleanup(config, first["run_id"], confirmed=True)
    assert cleaned["cleanup"]["snapshot_removed"]
    assert not Path(first["snapshot_directory"]).exists()
    assert Path(second["snapshot_directory"]).exists()
    assert Path(first["results_directory"]).is_dir()
    assert inspect_run(config, first["run_id"])["snapshot_present"] is False


def test_cli_investigation_and_existing_interfaces(
    config_file: Path,
    config: Config,
    atlassian: tuple,
    investigator: FakeInvestigator,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Expose investigation runs through the CLI's existing inspection interfaces."""
    from dev_agent.cli import main

    config_file.write_text(
        config_file.read_text()
        + """
[projects.example.atlassian]
site_url = "https://example.atlassian.net"
email_env = "DEV_AGENT_TEST_EMAIL"
token_env = "DEV_AGENT_TEST_TOKEN"
[projects.example.context]
instructions = ["AGENTS.md"]
"""
    )
    assert (
        main(
            [
                "--config",
                str(config_file),
                "investigate",
                "TASK-123",
                "--project",
                "example",
                "--dry-run",
            ]
        )
        == 0
    )
    assert '"dry_run": true' in capsys.readouterr().out
    assert not config.runs_root.exists()
    record = investigate(
        investigation_config(config, atlassian[0]),
        "example",
        "TASK-123",
        provider=atlassian[0],
        runner=investigator,
    )
    for command in (
        ["projects", "list"],
        ["runs", "list"],
        ["runs", "show", record["run_id"]],
        ["inspect", record["run_id"]],
        ["results", record["run_id"]],
    ):
        assert main(["--config", str(config_file), *command]) == 0
        assert capsys.readouterr().out
    assert main(["--config", str(config_file), "verify", record["run_id"]]) == 2
    assert "cannot be implementation-verified" in capsys.readouterr().err
    assert main(["--config", str(config_file), "cleanup", record["run_id"], "--yes"]) == 0
    assert not Path(record["snapshot_directory"]).exists()


def test_known_credentials_redacted_from_agent_artifacts(
    config: Config,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Remove provider-known test credentials from persisted output and exports."""
    import os

    provider = atlassian[0]
    secret = os.environ["DEV_AGENT_TEST_TOKEN"]

    def output(value: dict) -> dict:
        """Insert the synthetic credential into advisory output to exercise redaction."""
        value["executive_summary"] = secret
        return value

    investigator.output = output
    record = investigate(
        investigation_config(config, provider),
        "example",
        "TASK-123",
        provider=provider,
        runner=investigator,
    )
    assert record["outcome"] == "investigated"
    canonical = Store(config.runs_root).directory(record["run_id"])
    for directory in (canonical, Path(record["results_directory"])):
        for name in ("investigation.json", "investigation.md"):
            assert secret not in (directory / name).read_text()
    assert secret not in (canonical / "agent/final-response.md").read_text()


def test_unqualified_suitability_refused_with_omissions(
    config: Config,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Reject a suitability claim that overlooks retrieval or context omissions."""
    provider = atlassian[0]
    provider.config = replace(provider.config, search_confluence=False)

    def output(value: dict) -> dict:
        """Produce an apparently actionable assessment that ignores the omission."""
        value.update(
            suitability="suitable",
            recommended_next_action="Suitable for autonomous implementation.",
        )
        for factor in value["delivery_factors"]:
            factor["uncertain"] = False
        for criterion in value["criteria"]:
            criterion.update(status="not_satisfied", gaps=["Missing acceptance behaviour."])
        return value

    investigator.output = output
    record = investigate(
        investigation_config(config, provider),
        "example",
        "TASK-123",
        provider=provider,
        runner=investigator,
    )
    assert record["outcome"] == "invalid_investigation"
    assert "omissions" in record["error"]


def test_snapshot_context_uses_exact_baseline_hash(
    config: Config,
    repo: Path,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Tie examined work and context documents to exact committed source hashes."""
    import hashlib

    original = (repo / "AGENTS.md").read_bytes()
    (repo / "AGENTS.md").write_text("Dirty instruction must not reach model.\n")
    record = investigate(
        investigation_config(config, atlassian[0]),
        "example",
        "TASK-123",
        provider=atlassian[0],
        runner=investigator,
    )
    sources = json.loads((Path(record["results_directory"]) / "sources.json").read_text())
    context = sources["context"][0]
    assert context["commit"] == record["baseline_commit"]
    assert context["content_sha256"] == hashlib.sha256(original).hexdigest()
    assert (repo / "AGENTS.md").read_text().startswith("Dirty instruction")


def test_changed_snapshot_cleanup_requires_force(
    config: Config,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Preserve a modified investigation snapshot until explicitly forced."""
    config = investigation_config(config, atlassian[0])
    record = investigate(config, "example", "TASK-123", provider=atlassian[0], runner=investigator)
    root = Path(record["snapshot_directory"])
    file = root / "hello.py"
    file.chmod(0o644)
    file.write_text("Human investigation note.\n")
    with pytest.raises(DevAgentError, match="--force"):
        cleanup(config, record["run_id"], confirmed=True)
    assert file.read_text() == "Human investigation note.\n"
    with pytest.raises(DevAgentError, match="confirmation"):
        cleanup(config, record["run_id"], confirmed=False, force=True)
    assert root.exists()
    cleanup(config, record["run_id"], confirmed=True, force=True)
    assert not root.exists()


def test_agent_timeout_is_agent_failure(
    config: Config,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Preserve timeout evidence while declining the agent's assessment."""
    investigator.run = Mock(return_value=ProcessResult(-15, 10.0, timed_out=True))
    record = investigate(
        investigation_config(config, atlassian[0]),
        "example",
        "TASK-123",
        provider=atlassian[0],
        runner=investigator,
    )
    assert record["outcome"] == "agent_failed"
    assert record["agent"]["timed_out"] is True
    assert record["agent"]["exit_code"] == -15
    assert Path(record["snapshot_directory"]).is_dir()


def test_snapshot_cleanup_does_not_follow_substituted_symlink(
    config: Config,
    repo: Path,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Refuse removal through a substituted snapshot root symlink."""
    from dev_agent.repository.snapshot import remove_snapshot

    config = investigation_config(config, atlassian[0])
    record = investigate(config, "example", "TASK-123", provider=atlassian[0], runner=investigator)
    root = Path(record["snapshot_directory"])
    remove_snapshot(root)
    root.symlink_to(repo, target_is_directory=True)
    with pytest.raises(DevAgentError, match="symlink"):
        cleanup(config, record["run_id"], confirmed=True, force=True)
    assert (repo / "hello.py").read_text() == "VALUE = 1\n"


def test_baseline_embedded_credentials_refused_before_agent(
    config: Config,
    repo: Path,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Prevent a snapshot with known credentials from reaching the agent."""
    import os

    (repo / "accidental.txt").write_text(os.environ["DEV_AGENT_TEST_TOKEN"])
    git(repo, "add", "accidental.txt")
    git(repo, "commit", "-m", "synthetic credential")
    record = investigate(
        investigation_config(config, atlassian[0]),
        "example",
        "TASK-123",
        provider=atlassian[0],
        runner=investigator,
    )
    assert record["outcome"] == "context_failed"
    assert investigator.called == 0
    assert "refusing to expose" in record["error"]
    assert not (Path(record["snapshot_directory"]) / "accidental.txt").exists()
