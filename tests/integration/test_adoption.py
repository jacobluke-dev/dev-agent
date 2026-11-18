"""Exercise legacy provenance, current candidate qualification and publication recovery."""

import json
import shutil
from pathlib import Path

import pytest

from dev_agent.cli import main
from dev_agent.config import load_config
from dev_agent.delivery.adoption import adopt
from dev_agent.delivery.publication import publish_run
from dev_agent.errors import DevAgentError
from dev_agent.integrations.atlassian import content_hash, requirement_units
from dev_agent.records import Store, atomic_json, new_id, now
from dev_agent.repository.git import git, source_state, worktrees
from dev_agent.verification.runs import verify
from tests.support.investigation import FakeReviewer
from tests.support.publication import Publishing, state_change
from tests.support.publication import publishing as publishing

type LegacyFixture = tuple[Publishing, FakeReviewer, bytes]


@pytest.fixture
def legacy(
    publishing: Publishing, prompt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LegacyFixture:
    """Model a completed schema-1 run with independently passing historical targeted checks."""
    config, record, _, _ = publishing
    record["schema_version"] = 1
    record.pop("instruction_compliance")
    record.pop("approved_scope")
    record["outcome"] = "verification_failed"
    record["verification_attempts"] = record["verification_attempts"][:1]
    store = Store(config.runs_root)
    store.save(record)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tasks").mkdir()
    shutil.copyfile(prompt, tmp_path / "tasks" / f"{record['task']}.md")
    reviewer = FakeReviewer()
    monkeypatch.setattr("dev_agent.delivery.review.read_only_runner", lambda: reviewer)
    original = (store.directory(record["run_id"]) / "record.json").read_bytes()
    return publishing, reviewer, original


def test_legacy_adoption_approval_and_publication_recovery(legacy: LegacyFixture) -> None:
    """Fresh checks and review qualify one sidecar, while repeated publication reuses its PR."""
    (config, record, root, remote), reviewer, original = legacy
    store = Store(config.runs_root)
    run_id = record["run_id"]
    dependency = root / "ignored/.venv/sparse.bin"
    dependency.parent.mkdir(parents=True)
    with dependency.open("wb") as stream:
        stream.truncate(3 * 1024**3)
    state = source_state(root)["fingerprint"]
    trees = worktrees(config.project("example").repository)
    adoption = adopt(config, run_id)
    checkpoint = adoption["run_id"]
    assert adoption["adoption"]["status"] == "awaiting_legacy_approval"
    assert adoption["adoption"]["historical_instruction_enforcement"] == "not_recorded"
    assert "instruction_compliance" not in adoption
    assert [a["profile"] for a in adoption["verification_attempts"]] == ["targeted", "full"]
    assert reviewer.calls == 1
    assert source_state(root)["fingerprint"] == state
    assert worktrees(config.project("example").repository) == trees
    with pytest.raises(DevAgentError, match="explicit legacy approval"):
        publish_run(config, checkpoint, confirmed=True)
    with pytest.raises(DevAgentError, match="was not recorded"):
        publish_run(config, run_id, confirmed=True)
    with pytest.raises(DevAgentError, match="differs"):
        adopt(config, run_id, accept_legacy="0" * 64)
    with pytest.raises(DevAgentError, match="Use adopt"):
        verify(config, run_id)
    assert adopt(config, run_id)["run_id"] == checkpoint
    assert reviewer.calls == 1
    adopt(config, run_id, accept_legacy=adoption["adoption"]["approval_sha256"])
    preview = publish_run(config, checkpoint, dry_run=True)["plan"]
    assert preview["blockers"] == []
    assert "Historical instruction enforcement was unrecorded" in preview["body"]
    assert not json.loads(remote.read_text()).get("pushes")
    state_change(remote, create_failure=True)
    failed = publish_run(config, checkpoint, confirmed=True, watch=False)["publication"]
    assert failed["failure_phase"] == "pull_request"
    state_change(remote, create_failure=False)
    final = publish_run(config, checkpoint, confirmed=True, watch=False)["publication"]
    reused = publish_run(config, checkpoint, confirmed=True, watch=False)["publication"]
    assert failed["commit_sha"] == final["commit_sha"] == reused["commit_sha"]
    assert final["pr_number"] == reused["pr_number"] == 7
    assert reused["ci_observations"][-1]["status"] == "passed"
    fake = json.loads(remote.read_text())
    assert fake["pushes"] == 1
    assert sum(c[:2] == ["pr", "create"] for c in fake["calls"]) == 2
    assert (store.directory(run_id) / "record.json").read_bytes() == original
    assert dependency.stat().st_size == 3 * 1024**3


def test_full_failure_and_retry(legacy: LegacyFixture, tmp_path: Path) -> None:
    """Block before review on full-check failure and repeat only the unfinished check."""
    (config, record, _, remote), reviewer, _ = legacy
    gate = tmp_path / "full-check"
    gate.write_text("#!/bin/sh\nexit 1\n")
    gate.chmod(0o700)
    config.path.write_text(
        config.path.read_text().replace(
            'name = "full"\nargv = ["/usr/bin/true"]', f'name = "full"\nargv = ["{gate}"]'
        )
    )
    config = load_config(config.path)
    candidate = adopt(config, record["run_id"])
    assert candidate["adoption"]["status"] == "verification_blocked"
    assert reviewer.calls == 0
    with pytest.raises(DevAgentError, match="without override"):
        publish_run(
            config,
            candidate["run_id"],
            confirmed=True,
            allow_incomplete_full=True,
            incomplete_full_reason="Unavailable",
        )
    gate.write_text("#!/bin/sh\nexit 0\n")
    resumed = adopt(config, record["run_id"])
    assert resumed["run_id"] == candidate["run_id"]
    assert resumed["adoption"]["status"] == "awaiting_legacy_approval"
    assert [a["status"] for a in resumed["verification_attempts"]] == ["passed", "failed", "passed"]
    assert reviewer.calls == 1
    assert not json.loads(remote.read_text()).get("pushes")


def test_source_changes_invalidate_approval(legacy: LegacyFixture) -> None:
    """A candidate edit needs a new audit, checks, review and approval."""
    (config, record, root, remote), reviewer, _ = legacy
    run_id = record["run_id"]
    candidate = adopt(config, run_id)
    old = candidate["adoption"]["approval_sha256"]
    adopt(config, run_id, accept_legacy=old)
    (root / "new file.txt").write_text("new candidate\n")
    with pytest.raises(DevAgentError, match="current evidence"):
        publish_run(config, candidate["run_id"], confirmed=True)
    with pytest.raises(DevAgentError, match="stale"):
        adopt(config, run_id, accept_legacy=old)
    candidate = adopt(config, run_id)
    assert candidate["adoption"]["approval_sha256"] != old
    assert len(candidate["verification_attempts"]) == 4 and reviewer.calls == 2
    assert not json.loads(remote.read_text()).get("pushes")


@pytest.mark.parametrize(
    "failure", ["out_of_scope", "instruction", "staged", "recorded_failure", "failed_outcome"]
)
def test_adoption_refuses_invalid_scope_and_history(legacy: LegacyFixture, failure: str) -> None:
    """Do not adopt unknown source scope, changed authority, staged files or failed provenance."""
    (config, record, root, _), reviewer, _ = legacy
    run_id = record["run_id"]
    if failure == "out_of_scope":
        (root / "unapproved.txt").write_text("unexpected\n")
    elif failure == "instruction":
        (root / "AGENTS.md").write_text("changed instruction\n")
    elif failure == "staged":
        git(root, "add", "hello.py")
    else:
        store = Store(config.runs_root)
        original = store.load(run_id)
        if failure == "recorded_failure":
            original["instruction_compliance"] = {"status": "failed"}
        else:
            original["outcome"] = "instructions_failed"
        store.save(original)
    with pytest.raises(DevAgentError):
        adopt(config, run_id)
    assert reviewer.calls == 0


def test_invalid_review_never_qualifies(legacy: LegacyFixture) -> None:
    """Reject omissions in the independent review even when current tests pass."""
    (config, record, _, _), reviewer, _ = legacy
    reviewer.invalid = True
    adoption = adopt(config, record["run_id"])
    assert adoption["adoption"]["status"] == "review_blocked"
    assert "approval_sha256" not in adoption["adoption"]
    with pytest.raises(DevAgentError, match="explicit legacy approval"):
        publish_run(config, adoption["run_id"], confirmed=True)


def test_existing_jira_snapshot_needs_no_prompt_file(legacy: LegacyFixture, tmp_path: Path) -> None:
    """Bind a preserved Jira snapshot to the same ticket and reject tampering."""
    (config, record, _, _), _, _ = legacy
    (tmp_path / "tasks" / f"{record['task']}.md").unlink()
    store = Store(config.runs_root)
    issue = {
        "source_id": "jira:" + record["task"],
        "title": "Retained task",
        "description": "Use the expected value.",
        "acceptance_criteria": {"customfield_1": ["VALUE equals 2."]},
    }
    snapshot = {"task_id": record["task"], "issue": issue, "requirements": requirement_units(issue)}
    saved = new_id("example", record["task"])
    directory = store.create(
        {
            "schema_version": 2,
            "run_id": saved,
            "kind": "investigation",
            "task": record["task"],
            "project": record["project"],
            "repository": record["repository"],
            "outcome": "investigated",
            "requirements_sha256": content_hash(snapshot),
            "created_at": now(),
            "cleanup": None,
        }
    )
    filename = directory / "requirements-snapshot.json"
    atomic_json(filename, snapshot | {"requirements": []})
    with pytest.raises(DevAgentError, match="differs"):
        adopt(config, record["run_id"], requirements_run=saved)
    atomic_json(filename, snapshot)
    args = ["--config", str(config.path), "adopt", record["run_id"], "--requirements-run", saved]
    assert main(args) == 0
    state = adopt(config, record["run_id"])
    assert state["adoption"]["requirements"]["provenance"]["run_id"] == saved
    assert state["adoption"]["status"] == "awaiting_legacy_approval"


@pytest.mark.parametrize("existing", [False, True])
def test_adoption_prepares_node_workspace_before_full_checks(
    legacy: LegacyFixture, existing: bool
) -> None:
    """Use the configured installer before type checking, with or without an existing directory."""
    from dev_agent.config import Prerequisites
    from tests.support.runs import configured, python_check

    (config, original, root, _), reviewer, before = legacy
    install = python_check(
        "workspace-install",
        "from pathlib import Path; p=Path('ignored/node_modules/.bin/tsc'); "
        "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('installed'); "
        "Path('ignored/install-count').open('a').write('1')",
    )
    full = python_check(
        "full",
        "from pathlib import Path; "
        "assert Path('ignored/node_modules/.bin/tsc').read_text() == 'installed'",
    )
    untouched = root / "ignored/node_modules/keep.txt"
    if existing:
        untouched.parent.mkdir(parents=True)
        untouched.write_text("keep this environment")
    config = configured(
        config,
        preparation=(install,),
        prerequisites=Prerequisites(paths=("ignored/node_modules/.bin/tsc",)),
        verification_profiles={
            "targeted": config.project("example").checks("targeted")[1],
            "full": (full,),
        },
    )
    result = adopt(config, original["run_id"])
    assert result["adoption"]["status"] == "awaiting_legacy_approval"
    target, full_attempt = result["verification_attempts"]
    assert target["preparation"]["status"] == "passed"
    assert full_attempt["preparation"]["reuse"] == "current_invocation"
    assert full_attempt["checks"][0]["status"] == "passed"
    assert (root / "ignored/install-count").read_text() == "1"
    if existing:
        assert untouched.read_text() == "keep this environment"
    assert reviewer.calls == 1
    path = Store(config.runs_root).directory(original["run_id"]) / "record.json"
    assert path.read_bytes() == before


@pytest.mark.parametrize("interrupted", [False, True])
def test_adoption_retries_incomplete_automatic_preparation(
    legacy: LegacyFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    """Retain failed setup evidence and allow a later attempt on the same adoption."""
    from dev_agent.process import ProcessInterrupted, ProcessResult
    from dev_agent.verification import commands
    from tests.support.runs import configured, python_check

    (config, original, root, _), reviewer, _ = legacy
    gate = tmp_path / "allow-install"
    install = python_check(
        "setup",
        "from pathlib import Path; "
        f"assert Path({str(gate)!r}).exists(); "
        "Path('ignored').mkdir(exist_ok=True); Path('ignored/ready').touch()",
    )
    config = configured(config, preparation=(install,))
    if interrupted:
        real_execute = commands.execute

        def interrupt_install(*args: object, **kwargs: object) -> ProcessResult:
            """Interrupt only the first setup attempt."""
            if not gate.exists():
                raise ProcessInterrupted(ProcessResult(-15, 0.01))
            return real_execute(*args, **kwargs)

        monkeypatch.setattr(commands, "execute", interrupt_install)
    first = adopt(config, original["run_id"])
    failed = first["verification_attempts"][0]
    assert first["adoption"]["status"] == "preparation_blocked"
    assert failed["preparation"]["status"] == ("interrupted" if interrupted else "failed")
    assert all(check["status"] == "not_run" for check in failed["checks"])
    assert reviewer.calls == 0
    gate.touch()
    second = adopt(config, original["run_id"])
    assert second["adoption"]["status"] == "awaiting_legacy_approval"
    assert second["run_id"] == first["run_id"]
    assert second["verification_attempts"][0] == failed
    assert (root / "ignored/ready").is_file() and reviewer.calls == 1
