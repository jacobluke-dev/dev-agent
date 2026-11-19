"""Exercise legacy provenance, current candidate qualification and publication recovery."""

import json
import shutil
from pathlib import Path

import pytest

from dev_agent.cli import main
from dev_agent.config import load_config
from dev_agent.delivery.adoption import adopt
from dev_agent.delivery.publication import ci_run, publish_run
from dev_agent.errors import DevAgentError
from dev_agent.integrations.atlassian import content_hash, requirement_units
from dev_agent.records import Store, atomic_json, new_id, now
from dev_agent.repository.git import git, source_state, worktrees
from dev_agent.verification.runs import verify
from tests.support.investigation import FakeReviewer
from tests.support.publication import Publishing, enable_remote_ci, state_change
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
    package_rules = root / "ignored/node_modules/next/AGENTS.md"
    package_rules.parent.mkdir(parents=True)
    package_rules.write_text("Package-local guidance installed by preparation.")
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
    assert package_rules.relative_to(root).as_posix() not in {
        entry["path"] for entry in preview["approved_files"]
    }
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


def test_remote_ci_adoption_requalifies_legacy_source_and_recovers_ready_publication(
    legacy: LegacyFixture,
) -> None:
    """Bind fresh review and targeted evidence to a new policy before a recoverable normal PR."""
    (local, original, root, remote), reviewer, original_bytes = legacy
    run_id = original["run_id"]
    old = adopt(local, run_id)
    approval = old["adoption"]["approval_sha256"]
    config = enable_remote_ci(local, remote)
    with pytest.raises(DevAgentError, match="stale"):
        adopt(config, run_id, accept_legacy=approval)
    current = adopt(config, run_id)
    checkpoint = current["run_id"]
    assert checkpoint == old["run_id"] and reviewer.calls == 2
    assert current["adoption"]["historical_instruction_enforcement"] == "not_recorded"
    assert [a["profile"] for a in current["verification_attempts"]] == [
        "targeted",
        "full",
        "targeted",
    ]
    assert current["adoption"]["superseded_candidates"][0]["verification_policy"]["mode"] == (
        "local-full"
    )
    with pytest.raises(DevAgentError, match="explicit legacy approval"):
        publish_run(config, checkpoint, pr_mode="ready", confirmed=True)
    adopt(config, run_id, accept_legacy=current["adoption"]["approval_sha256"])
    blocked = publish_run(config, checkpoint, pr_mode="draft", dry_run=True)["plan"]
    assert any("draft PR" in reason for reason in blocked["blockers"])
    plan = publish_run(config, checkpoint, pr_mode="ready", dry_run=True)["plan"]
    assert not plan["blockers"]
    assert plan["verification"]["profiles"]["full"]["status"] == "unqualified"
    assert not json.loads(remote.read_text()).get("pushes")
    state_change(remote, create_failure=True)
    failed = publish_run(config, checkpoint, pr_mode="ready", confirmed=True)["publication"]
    assert failed["failure_phase"] == "pull_request"
    with pytest.raises(DevAgentError, match="refusing PR conversion"):
        publish_run(config, checkpoint, pr_mode="draft", confirmed=True)
    state_change(remote, create_failure=False, checks="absent")
    final = publish_run(config, checkpoint, pr_mode="ready", confirmed=True, watch=False)[
        "publication"
    ]
    assert failed["commit_sha"] == final["commit_sha"]
    assert not final["draft"] and final["pr_mode"] == "ready"
    assert final["ci_observations"][-1]["status"] == "missing"
    for status in ("pending", "failed", "skipped", "timed_out", "passed"):
        state_change(remote, checks=status)
        observation = ci_run(config, checkpoint)
        assert observation["status"] == status
        assert observation["commit_sha"] == final["commit_sha"]
    state_change(remote, check_pr=8)
    assert ci_run(config, checkpoint)["status"] == "missing"
    state_change(remote, check_pr=7, job_head="f" * 40)
    assert ci_run(config, checkpoint)["status"] == "unable_to_retrieve"
    # A config edit cannot change the meaning of an already published run's CI evidence.
    state_change(remote, checks="missing_job")
    assert ci_run(local, checkpoint)["status"] == "missing"
    state_change(remote, base="e" * 40, checks="passed", job_head=final["commit_sha"])
    assert ci_run(config, checkpoint)["status"] == "base_mismatch"
    plan = publish_run(config, checkpoint, pr_mode="ready", dry_run=True)["plan"]
    assert any("acknowledge-stale-base" in reason for reason in plan["blockers"])
    reviewed = publish_run(
        config, checkpoint, pr_mode="ready", acknowledge_stale_base=True, confirmed=True
    )["publication"]
    assert reviewed["commit_sha"] == final["commit_sha"]
    assert reviewed["ci_observations"][-1]["status"] == "passed"
    data = json.loads(remote.read_text())
    assert data["pushes"] == 1
    assert data["pull"]["number"] == 7
    assert not any(call[:2] in (["pr", "ready"], ["pr", "merge"]) for call in data["calls"])
    assert (
        Store(config.runs_root).directory(run_id) / "record.json"
    ).read_bytes() == original_bytes


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
    assert first["adoption"]["status"] == ("interrupted" if interrupted else "preparation_blocked")
    assert failed["preparation"]["status"] == ("interrupted" if interrupted else "failed")
    assert all(check["status"] == "not_run" for check in failed["checks"])
    assert reviewer.calls == 0
    with Store(config.runs_root).lock(original["run_id"]):
        pass
    gate.touch()
    second = adopt(config, original["run_id"])
    assert second["adoption"]["status"] == "awaiting_legacy_approval"
    assert second["run_id"] == first["run_id"]
    assert second["verification_attempts"][0] == failed
    assert (root / "ignored/ready").is_file() and reviewer.calls == 1


@pytest.mark.parametrize("signame", ["SIGTSTP", "SIGTERM"])
def test_adoption_cli_cancels_preparation_without_leaving_lock(
    legacy: LegacyFixture, tmp_path: Path, signame: str
) -> None:
    """Cancel a real foreground-style CLI job, reap its installer and save valid evidence."""
    import os
    import signal
    import subprocess
    import sys
    import time

    from dev_agent.delivery.adoption import summary
    from dev_agent.records import adoption_id

    (config, original, _, _), _, history = legacy
    store = Store(config.runs_root)
    run_id = original["run_id"]
    ready = tmp_path / "installer.pid"
    script = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    config.path.write_text(
        config.path.read_text()
        + '\n[[projects.example.preparation]]\nname = "fixture-install"\n'
        + f"argv = {json.dumps([sys.executable, '-c', script])}\n"
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dev_agent",
            "--config",
            str(config.path),
            "adopt",
            run_id,
            "--json",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        process_group=0,
    )
    installer = None
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() or not ready.read_text().strip():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        installer = int(ready.read_text())
        live = summary(store.load(adoption_id(run_id)))["recorded_in_progress"]
        assert live["phase"] == "preparing"
        assert live["command"]["controller_pid"] == proc.pid
        assert live["command"]["name"] == "fixture-install"
        assert Path(live["command"]["stderr_log"]).is_file()
        with pytest.raises(DevAgentError, match=f"Run {run_id} is active"):
            with store.lock(run_id):
                pass

        os.killpg(proc.pid, getattr(signal, signame))
        # Repeated cancellation during the bounded SIGTERM grace period cannot erase evidence.
        time.sleep(0.15)
        os.killpg(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 130, (stdout, stderr)
        assert json.loads(stdout)["status"] == "interrupted"
        assert not Path(f"/proc/{installer}").exists()
        with store.lock(run_id):
            state = store.load(adoption_id(run_id))
        attempt = state["verification_attempts"][-1]
        assert attempt["outcome"] == "interrupted" and attempt["phase"] == "finished"
        assert attempt["preparation"]["status"] == "interrupted"
        command = attempt["preparation"]["commands"][0]
        assert command["exit_code"] == -signal.SIGKILL
        assert command["finished_at"] and attempt["finished_at"]
        assert all(check["status"] == "not_run" for check in attempt["checks"])
        assert (store.directory(run_id) / "record.json").read_bytes() == history
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if installer and Path(f"/proc/{installer}").exists():
            try:
                os.killpg(installer, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_adoption_blocks_retry_when_installer_cleanup_is_uncertain(
    legacy: LegacyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release the advisory lock and retain an explicit guard against overlapping setup."""
    from unittest.mock import Mock

    from dev_agent.process import CLEANUP_ERROR, ProcessResult
    from tests.support.runs import configured, python_check

    (config, original, _, _), reviewer, _ = legacy
    config = configured(config, preparation=(python_check("fixture-install", "pass"),))
    install = Mock(
        return_value=ProcessResult(
            None, 0.5, timed_out=True, error=CLEANUP_ERROR, failure_kind="cleanup_incomplete"
        )
    )
    monkeypatch.setattr("dev_agent.verification.commands.execute", install)
    run_id = original["run_id"]
    first = adopt(config, run_id)
    attempt = first["verification_attempts"][-1]
    assert attempt["preparation"]["status"] == "uncertain"
    assert attempt["preparation"]["commands"][0]["failure_kind"] == "cleanup_incomplete"
    assert all(check["status"] == "not_run" for check in attempt["checks"])
    with Store(config.runs_root).lock(run_id):
        pass
    later = adopt(config, run_id)
    assert later["adoption"]["status"] == "preparation_blocked"
    assert later["verification_attempts"][-1]["preparation"]["status"] == "uncertain"
    install.assert_called_once()
    assert reviewer.calls == 0


def test_adoption_reconciles_interrupted_installer_after_empty_refusals(
    legacy: LegacyFixture, tmp_path: Path
) -> None:
    """Recover using the original installer receipt and preserve every prior attempt."""
    from copy import deepcopy

    from tests.support.runs import configured, python_check

    (config, original, _, _), reviewer, history = legacy
    gate = tmp_path / "setup-ok"
    command = python_check(
        "node-dependencies",
        f"from pathlib import Path; raise SystemExit(0 if Path({str(gate)!r}).exists() else 1)",
    )
    config = configured(config, preparation=(command,))
    run_id = original["run_id"]
    first = adopt(config, run_id)
    stale = first["verification_attempts"][-1]
    state = stale["preparation"]
    installer = state["commands"][0]
    assert installer["process"]["child"]["session"] == installer["process"]["child"]["pid"]
    # Model the last durable checkpoint before the previous controller died.
    state["status"] = "running"
    installer.update(status="running", exit_code=None, finished_at=None)
    stale.update(phase="preparing", outcome="planned", finished_at=None)
    refused = deepcopy(stale)
    refused["attempt_id"] += "-refused"
    refused.update(phase="finished", outcome="environment_failed")
    refused["preparation"] = {"status": "uncertain", "commands": []}
    first["verification_attempts"].append(refused)
    store = Store(config.runs_root)
    store.save(first)
    saved_history = deepcopy(first["verification_attempts"])

    gate.touch()
    with store.lock(run_id):
        with pytest.raises(DevAgentError, match="is active"):
            adopt(config, run_id)
    resumed = adopt(config, run_id)
    assert resumed["adoption"]["status"] == "awaiting_legacy_approval"
    assert resumed["verification_attempts"][: len(saved_history)] == saved_history
    retry = resumed["verification_attempts"][len(saved_history)]["preparation"]
    assert retry["recovery"]["decision"] == "retry"
    assert retry["status"] == "passed"
    assert reviewer.calls == 1
    assert (store.directory(run_id) / "record.json").read_bytes() == history


def test_advanced_adoption_push_requires_fresh_legacy_acceptance(legacy: LegacyFixture) -> None:
    """Qualify an already-published CI parent without rewriting the original legacy run."""
    (local, original, root, state), reviewer, original_bytes = legacy
    config = enable_remote_ci(local, state)
    path = root / ".github/workflows/ci.yml"
    path.write_text(path.read_text() + "# separate history\n")
    git(root, "add", ".github/workflows/ci.yml")
    git(root, "commit", "-m", "Previously published CI change")
    head = source_state(root)["head_commit"]
    with pytest.raises(DevAgentError, match="already be published"):
        adopt(config, original["run_id"], approve_head=head)
    state_change(state, head=head, checks="absent", workflows=False)
    candidate = adopt(config, original["run_id"], approve_head=head)
    assert candidate["adoption"]["candidate"]["previous_head"] == head
    assert reviewer.calls == 1
    args = (config, candidate["run_id"])
    with pytest.raises(DevAgentError, match="legacy approval"):
        publish_run(*args, branch_only=True)
    adopt(config, original["run_id"], accept_legacy=candidate["adoption"]["approval_sha256"])
    plan = publish_run(*args, branch_only=True)["plan"]
    assert not plan["blockers"]
    pub = publish_run(*args, branch_only=True, confirmed=True, plan_sha256=plan["plan_sha256"])
    assert pub["publication"]["status"] == "pushed"
    assert pub["adoption"]["historical_instruction_enforcement"] == "not_recorded"
    assert pub["publication"]["ci_observations"] == []
    assert (
        Store(config.runs_root).directory(original["run_id"]) / "record.json"
    ).read_bytes() == original_bytes
