"""Exercise delivery checkpoints with synthetic intake, isolated repositories and fake remotes.

The cases also protect the production MCP qualification gate before any implementation starts.
"""

import json
from pathlib import Path

import pytest

from dev_agent.cli import main
from dev_agent.config import Config, load_config
from dev_agent.delivery.publication import publish_run
from dev_agent.delivery.review import review
from dev_agent.delivery.workflow import deliver
from dev_agent.errors import DevAgentError
from dev_agent.integrations.atlassian import content_hash, requirement_units
from dev_agent.records import RunRecord, Store, now
from dev_agent.reporting.delivery import summary
from dev_agent.repository.git import git, output, status
from dev_agent.run_admin import inspect_run
from tests.support.investigation import FakeReviewer, accepted_output
from tests.support.publication import Publishing, enable_remote_ci, state_change
from tests.support.publication import publishing as publishing

type DeliveryFixture = tuple[Publishing, FakeReviewer, list[str]]


def intake_value(record: RunRecord) -> RunRecord:
    """Construct matching requirements, source provenance and actionable baseline assessment."""
    issue = {
        "source_id": "jira:" + record["task"],
        "title": "Use VALUE = 2",
        "description": "Set the maintained fixture VALUE to 2.",
        "acceptance_criteria": {},
        "url": "https://example.atlassian.net/browse/" + record["task"],
        "retrieved_at": now(),
        "revision": {"updated": "2026-01-01T00:00:00Z"},
    }
    issue["content_sha256"] = content_hash({k: v for k, v in issue.items() if k != "retrieved_at"})
    snapshot = {
        "schema_version": 1,
        "provider": "codex_atlassian_mcp",
        "task_id": record["task"],
        "issue": issue,
        "requirements": requirement_units(issue),
        "linked_issues": [],
        "confluence_documents": [],
        "warnings": [],
    }
    assessment = accepted_output(snapshot, record["baseline_commit"])
    assessment.update(
        suitability="suitable", recommended_next_action="Suitable for autonomous implementation."
    )
    for criterion in assessment["criteria"]:
        criterion.update(status="not_satisfied", gaps=["VALUE must become 2."], uncertainties=[])
    for factor in assessment["delivery_factors"]:
        factor.update(assessment="Bounded synthetic local change.", uncertain=False)
    return {
        "provider": "codex_atlassian_mcp",
        "requirements_snapshot": snapshot,
        "assessment": assessment,
    }


@pytest.fixture
def delivery_pipeline(
    publishing: Publishing, monkeypatch: pytest.MonkeyPatch
) -> tuple[Publishing, FakeReviewer, list[str]]:
    """Wire synthetic intake and review into a local delivery fixture with intercepted pushes."""
    config, old, root, state = publishing
    reviewer = FakeReviewer()
    intakes = []

    def intake(config: Config, record: RunRecord, directory: Path, bundle: object) -> RunRecord:
        """Count intake calls and build the fixture's validated requirements and assessment."""
        intakes.append(record["run_id"])
        return intake_value(record)

    monkeypatch.setattr("dev_agent.delivery.workflow.mcp_intake", intake)
    monkeypatch.setattr(
        "dev_agent.delivery.workflow.review", lambda c, r, s, b: review(c, r, s, b, runner=reviewer)
    )

    def fake_push(client: object, branch: str, commit: str) -> None:
        """Record the intended fixture ref update or inject a controlled push failure."""
        assert branch.startswith("dev-agent/") and branch != old["branch"]
        data = json.loads(state.read_text())
        data["pushes"] = data.get("pushes", 0) + 1
        if data.get("push_failure"):
            raise DevAgentError("Synthetic push failure")
        data["head"] = commit
        state.write_text(json.dumps(data))

    monkeypatch.setattr("dev_agent.delivery.publication.push", fake_push)
    return publishing, reviewer, intakes


def test_unqualified_mcp_blocks_before_agent_without_rest_fallback(publishing: Publishing) -> None:
    """Stop live intake without REST fallback, implementation or GitHub publication."""
    config, old, root, state = publishing
    before = status(config.project("example").repository)
    result = deliver(config, "example", "TASK-42", confirmed=True, watch=True)
    assert result["outcome"] == "mcp_unavailable"
    assert result["implementation_run_id"] is None
    assert len(Store(config.runs_root).list()) == 2
    assert status(config.project("example").repository) == before
    assert not json.loads(state.read_text()).get("pushes")
    assert summary(config, result)["generated_plan"] is None
    assert inspect_run(config, result["run_id"])["status"] == "mcp_unavailable"
    assert Path(result["results_directory"], "report.json").is_file()


def test_dry_run_is_side_effect_free_and_reports_real_blocker(publishing: Publishing) -> None:
    """Report the actual intake blocker without changing storage or calling GitHub."""
    config, old, root, state = publishing
    before = set(config.runs_root.iterdir())
    result = deliver(config, "example", "TASK-42", dry_run=True, confirmed=True)
    assert result["status"] == "mcp_unavailable"
    assert set(config.runs_root.iterdir()) == before
    assert not json.loads(state.read_text())["calls"]


def test_confirm_does_not_override_mcp_or_instruction_gate(publishing: Publishing) -> None:
    """Keep mandatory local instructions in force even when publication was requested."""
    config, old, root, state = publishing
    source = config.project("example").repository
    (source / "AGENTS.md").unlink()
    git(source, "add", "AGENTS.md")
    git(source, "commit", "-m", "Synthetic missing instructions")
    with pytest.raises(DevAgentError, match="AGENTS.md"):
        deliver(config, "example", "TASK-42", confirmed=True)
    assert len(Store(config.runs_root).list()) == 1


def test_no_confirmation_implements_reviews_and_previews_without_publication(
    delivery_pipeline: DeliveryFixture,
) -> None:
    """Reach a reviewed local candidate and retain a preview when confirmation is absent."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    result = deliver(config, "example", "TASK-42")
    assert result["outcome"] == "awaiting_confirmation", result["blockers"]
    child = Store(config.runs_root).load(result["implementation_run_id"])
    assert child["implementation_status"] == "agent_completed"
    assert child["independent_review"]["status"] == "passed"
    assert len(child["verification_attempts"]) == 2
    assert not child.get("publication")
    assert reviewer.calls == 1 and len(intakes) == 1
    assert "Use the existing package style." not in Path(result["generated_plan"]).read_text()
    assert output(Path(child["worktree"]), "rev-parse", "HEAD") == old["baseline_commit"]
    assert not json.loads(state.read_text()).get("pushes")
    assert Path(result["publication_plan"]).is_file()


def test_confirmed_delivery_and_read_only_ci_resumption(delivery_pipeline: DeliveryFixture) -> None:
    """Resume published delivery without repeating implementation or PR creation."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    first = deliver(config, "example", "TASK-42", confirmed=True, watch=True)
    assert first["outcome"] == "delivered", first["blockers"]
    info = summary(config, first)
    assert info["commit_sha"] and info["draft_pr_url"].endswith("/pull/7")
    second = deliver(config, "example", "TASK-42", resume=first["run_id"], watch=True)
    assert second["outcome"] == "delivered", second["blockers"]
    assert second["implementation_run_id"] == first["implementation_run_id"]
    assert len(intakes) == 1 and reviewer.calls == 1
    data = json.loads(state.read_text())
    assert data["pushes"] == 1
    assert len([c for c in data["calls"] if c[:2] == ["pr", "create"]]) == 1
    assert status(root)  # Existing independent worktree remains dirty and uncommitted.
    assert output(root, "rev-parse", "HEAD") == old["baseline_commit"]


def test_ready_remote_ci_requires_review_and_exact_expected_checks(
    delivery_pipeline: DeliveryFixture,
) -> None:
    """Deliver after targeted verification and independent review, then wait for required CI."""
    (local, _, _, state), reviewer, intakes = delivery_pipeline
    config = enable_remote_ci(local, state)
    preview = deliver(config, "example", "TASK-42", dry_run=True, pr_mode="draft")
    assert any("draft PR" in blocker for blocker in preview["blockers"])
    result = deliver(config, "example", "TASK-42", pr_mode="ready")
    assert result["outcome"] == "awaiting_confirmation", result["blockers"]
    child = Store(config.runs_root).load(result["implementation_run_id"])
    assert child["independent_review"]["status"] == "passed"
    assert [attempt["profile"] for attempt in child["verification_attempts"]] == ["targeted"]
    assert not json.loads(state.read_text()).get("pushes")
    with pytest.raises(DevAgentError, match="PR mode differs"):
        deliver(config, "example", "TASK-42", pr_mode="draft", resume=result["run_id"])
    state_change(state, checks="absent")
    result = deliver(
        config,
        "example",
        "TASK-42",
        pr_mode="ready",
        resume=result["run_id"],
        confirmed=True,
        watch=True,
    )
    assert result["outcome"] == "ci_not_passed", result["blockers"]
    assert summary(config, result)["ci_status"] == "monitoring_timeout"
    state_change(state, checks="passed")
    resumed = deliver(config, "example", "TASK-42", pr_mode="ready", resume=result["run_id"])
    assert resumed["outcome"] == "delivered", resumed["blockers"]
    details = summary(config, resumed)
    assert details["pr_url"].endswith("/pull/7") and details["draft_pr_url"] is None
    assert len(intakes) == reviewer.calls == 1
    calls = json.loads(state.read_text())["calls"]
    creates = [call for call in calls if call[:2] == ["pr", "create"]]
    assert len(creates) == 1 and "--draft" not in creates[0]
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


def test_publication_failure_recovery_reuses_commit_and_pr(
    delivery_pipeline: DeliveryFixture,
) -> None:
    """Recover once GitHub accepts draft creation using the original commit and branch push."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    data = json.loads(state.read_text())
    data["create_failure"] = True
    state.write_text(json.dumps(data))
    first = deliver(config, "example", "TASK-42", confirmed=True)
    assert first["outcome"] == "publication_failed", first["blockers"]
    commit = summary(config, first)["commit_sha"]
    data = json.loads(state.read_text())
    data["create_failure"] = False
    state.write_text(json.dumps(data))
    second = deliver(config, "example", "TASK-42", confirmed=True, resume=first["run_id"])
    assert second["outcome"] == "delivered", second["blockers"]
    assert summary(config, second)["commit_sha"] == commit
    assert reviewer.calls == 1 and len(intakes) == 1
    assert json.loads(state.read_text())["pushes"] == 1


def test_full_failure_blocks_review_and_publication(delivery_pipeline: DeliveryFixture) -> None:
    """Stop before review and publication when the full profile fails."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    config.path.write_text(
        config.path.read_text().replace(
            'name = "full"\nargv = ["/usr/bin/true"]', 'name = "full"\nargv = ["/usr/bin/false"]'
        )
    )
    config = load_config(config.path)
    result = deliver(config, "example", "TASK-42", confirmed=True)
    assert result["outcome"] == "verification_blocked", result["blockers"]
    assert reviewer.calls == 0
    assert not json.loads(state.read_text()).get("pushes")


@pytest.mark.parametrize("mode", ["findings", "invalid", "mutation"])
def test_independent_review_blocks_bad_evidence(
    delivery_pipeline: DeliveryFixture, mode: str
) -> None:
    """Block publication on failed, incomplete or integrity-invalid review evidence."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    if mode == "findings":
        reviewer.verdict = "changes_requested"
    if mode == "invalid":
        reviewer.invalid = True
    if mode == "mutation":
        reviewer.mutate = True
    result = deliver(config, "example", "TASK-42", confirmed=True)
    assert result["outcome"] == "review_blocked", result["blockers"]
    child = Store(config.runs_root).load(result["implementation_run_id"])
    if mode == "mutation":
        assert child["independent_review"]["status"] == "security_failed"
    with pytest.raises(DevAgentError, match="independent review"):
        publish_run(config, child["run_id"], confirmed=True)
    resumed = deliver(config, "example", "TASK-42", confirmed=True, resume=result["run_id"])
    assert resumed["outcome"] == "review_blocked"
    assert reviewer.calls == 1


def test_no_second_agent_after_failed_implementation_checkpoint(
    delivery_pipeline: DeliveryFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse to start a second agent after an interrupted candidate run is recorded."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    original = __import__("dev_agent.delivery.implementation", fromlist=["implement"]).implement

    def interrupted_engine(*args: object, **kwargs: object) -> RunRecord:
        """Leave an implementation record that appears interrupted during agent execution."""
        child = original(*args, **kwargs)
        child.update(phase="implementing", implementation_status="running")
        Store(config.runs_root).save(child)
        raise KeyboardInterrupt

    monkeypatch.setattr("dev_agent.delivery.workflow.implement", interrupted_engine)
    first = deliver(config, "example", "TASK-42", confirmed=True)
    assert first["outcome"] == "interrupted"
    monkeypatch.setattr(
        "dev_agent.delivery.workflow.implement",
        lambda *a, **k: pytest.fail("agent must not restart"),
    )
    second = deliver(config, "example", "TASK-42", confirmed=True, resume=first["run_id"])
    assert second["outcome"] == "implementation_blocked"
    assert reviewer.calls == 0 and len(intakes) == 1


def test_resume_refuses_wrong_identity_or_changed_artifacts(
    delivery_pipeline: DeliveryFixture,
) -> None:
    """Reject changed brief content or a mismatched ticket on resumption."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    first = deliver(config, "example", "TASK-42")
    with pytest.raises(DevAgentError, match="identity"):
        deliver(config, "example", "TASK-99", resume=first["run_id"])
    Path(first["generated_plan"]).write_text("Tampered brief\n")
    with pytest.raises(DevAgentError, match="artifact changed"):
        deliver(config, "example", "TASK-42", confirmed=True, resume=first["run_id"])


def test_retained_review_evidence_is_required_for_individual_publish(
    delivery_pipeline: DeliveryFixture,
) -> None:
    """Require intact retained review artifacts even through the standalone publish command."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    first = deliver(config, "example", "TASK-42")
    store = Store(config.runs_root)
    child = store.load(first["implementation_run_id"])
    review_dir = (
        store.directory(child["run_id"]) / "review" / child["independent_review"]["attempt_id"]
    )
    (review_dir / "review.json").unlink()
    with pytest.raises(DevAgentError, match="evidence is missing"):
        publish_run(config, child["run_id"], confirmed=True)


def test_cli_progress_and_non_success_on_unavailable_mcp(
    publishing: Publishing, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose progress on stderr and a non-success, machine-readable blocker on stdout."""
    config, old, root, state = publishing
    assert (
        main(
            [
                "--config",
                str(config.path),
                "deliver",
                "TASK-42",
                "--project",
                "example",
                "--draft",
                "--confirm",
                "--watch",
            ]
        )
        == 13
    )
    captured = capsys.readouterr()
    assert "retrieving_mcp_requirements" in captured.err
    assert "execution_id" in captured.out and "mcp_unavailable" in captured.out


@pytest.mark.parametrize("ci", ["pending", "failed", "skipped", "absent"])
def test_ci_nonpasses_never_claim_delivery_success(
    delivery_pipeline: DeliveryFixture, ci: str
) -> None:
    """Prevent incomplete or unsuccessful CI states from producing a delivered outcome."""
    (config, old, root, state), reviewer, intakes = delivery_pipeline
    data = json.loads(state.read_text())
    data["checks"] = ci
    state.write_text(json.dumps(data))
    result = deliver(config, "example", "TASK-42", confirmed=True, watch=False)
    assert result["outcome"] == "ci_not_passed"
    assert summary(config, result)["ci_status"] != "passed"
    assert result["blockers"]


def test_review_copy_refuses_symlinked_parents(tmp_path: Path) -> None:
    """Prevent candidate review from copying through a symlinked parent directory."""
    from dev_agent.delivery.review import read_candidate

    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.py").write_text("synthetic private data")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read_candidate(root, "linked/file.py", 1024)


@pytest.mark.parametrize("corruption", ["hash", "identity", "provider"])
def test_invalid_ticket_evidence_stops_before_implementation(
    publishing: Publishing, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    """Refuse intake provenance with mismatched task, provider or content hash."""
    config, old, root, state = publishing

    def intake(config: Config, record: RunRecord, directory: Path, bundle: object) -> RunRecord:
        """Corrupt exactly the selected field in the synthetic intake envelope."""
        value = intake_value(record)
        if corruption == "hash":
            value["requirements_snapshot"]["issue"]["content_sha256"] = "invalid"
        if corruption == "identity":
            value["requirements_snapshot"]["task_id"] = "TASK-999"
        if corruption == "provider":
            value["provider"] = "rest"
        return value

    monkeypatch.setattr("dev_agent.delivery.workflow.mcp_intake", intake)
    result = deliver(config, "example", "TASK-42", confirmed=True)
    assert result["outcome"] == "invalid_intake"
    assert result["implementation_run_id"] is None


@pytest.mark.parametrize("abandoned", [False, True])
def test_delivery_retries_preparation_before_agent_on_same_checkpoint(
    delivery_pipeline: DeliveryFixture, tmp_path: Path, abandoned: bool
) -> None:
    """Resume a blocked setup while keeping the child run, worktree and first attempt evidence."""
    from tests.support.runs import configured, python_check

    (config, _, _, remote), reviewer, intakes = delivery_pipeline
    gate = tmp_path / "allow-install"
    check = python_check(
        "setup",
        "from pathlib import Path; "
        f"assert Path({str(gate)!r}).exists(); "
        "Path('ignored').mkdir(exist_ok=True); f=Path('ignored/setup-count'); "
        "f.open('a').write('1'); print('installed')",
    )
    config = configured(config, preparation=(check,))
    first = deliver(config, "example", "TASK-42")
    assert first["outcome"] == "preparation_blocked", first["blockers"]
    store = Store(config.runs_root)
    child_id = first["implementation_run_id"]
    blocked = store.load(child_id)
    assert blocked["preparation"]["status"] == "failed" and blocked["agent"]["status"] == "pending"
    if abandoned:
        blocked.update(phase="preparing", outcome="planned", finished_at=None)
        blocked["preparation"]["status"] = "running"
        blocked["preparation"]["commands"][0].update(
            status="running", exit_code=None, finished_at=None
        )
        store.save(blocked)
    root = Path(blocked["worktree"])
    gate.touch()
    resumed = deliver(config, "example", "TASK-42", resume=first["run_id"])
    assert resumed["outcome"] == "awaiting_confirmation", resumed["blockers"]
    assert resumed["implementation_run_id"] == child_id
    child = store.load(child_id)
    assert child["preparation_history"][0]["state"] == blocked["preparation"]
    assert child["preparation"]["recovery"]["decision"] == "retry"
    assert child["verification_attempts"][-1]["preparation"]["reuse"] == "current_invocation"
    assert (root / "ignored/setup-count").read_text() == "1"
    projection = json.loads((Path(child["results_directory"]) / "report.json").read_text())
    exported = projection["preparation"]["commands"][0]["export_stdout"]
    assert (Path(child["results_directory"]) / exported).read_text() == "installed\n"
    assert reviewer.calls == 1 and len(intakes) == 1
    assert not json.loads(remote.read_text()).get("pushes")
