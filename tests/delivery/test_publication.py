"""Exercise draft publication with real temporary Git commits and fake GitHub responses.

The suite intercepts pushes and tests eligibility, recovery and exact-commit CI handling.
"""

import json
from pathlib import Path

import pytest

from dev_agent.cli import main
from dev_agent.config import load_config
from dev_agent.delivery.publication import publish_run, remote_repository
from dev_agent.errors import DevAgentError
from dev_agent.integrations.github import GitHub, GitHubError
from dev_agent.records import Store
from dev_agent.repository.git import git, output, source_state, status
from dev_agent.verification.runs import verify
from tests.support.publication import Publishing, state_change
from tests.support.publication import publishing as publishing


def test_preview_and_unconfirmed_have_no_git_or_record_side_effects(publishing: Publishing) -> None:
    """Validate preview without changing refs, source, canonical metadata or the remote."""
    config, record, root, state = publishing
    store = Store(config.runs_root)
    before_record = (store.directory(record["run_id"]) / "record.json").read_bytes()
    before = source_state(root)
    refs = output(root, "show-ref")
    for options in ({"dry_run": True}, {}):
        result = publish_run(config, record["run_id"], **options)
        assert result["plan"]["blockers"] == []
        assert {f["path"] for f in result["plan"]["approved_files"]} == {"hello.py", "new file.txt"}
    assert before["fingerprint"] == source_state(root)["fingerprint"]
    assert output(root, "show-ref") == refs
    assert (store.directory(record["run_id"]) / "record.json").read_bytes() == before_record
    assert not json.loads(state.read_text()).get("pushes")
    assert not (store.directory(record["run_id"]) / "publication").exists()


def test_confirm_creates_commit_push_draft_and_retains_original_evidence(
    publishing: Publishing,
) -> None:
    """Publish reviewed changes as a draft while preserving the earlier evidence."""
    config, record, root, state = publishing
    original = json.loads(json.dumps(record))
    plan = publish_run(config, record["run_id"], dry_run=True)["plan"]
    result = publish_run(config, record["run_id"], confirmed=True, plan_sha256=plan["plan_sha256"])
    pub = result["publication"]
    assert pub["status"] == "published"
    assert pub["pr_url"].endswith("/pull/7") and pub["draft"]
    assert pub["commit_sha"] == output(root, "rev-parse", "HEAD")
    assert pub["ci_observations"][-1]["status"] == "passed"
    assert not status(root)
    for key in ("agent", "outcome", "verification_attempts", "instruction_compliance"):
        assert result[key] == original[key]
    assert (
        Path(result["results_directory"])
        / "publication"
        / pub["plans"][0]["attempt_id"]
        / "approved-files.json"
    ).is_file()
    assert "GitHub publication" in (Path(result["results_directory"]) / "summary.md").read_text()
    source = config.project("example").repository
    assert output(source, "branch", "--show-current") == "main"
    assert output(source, "rev-parse", "HEAD") == record["baseline_commit"]


def test_idempotence_and_partial_pr_failure_recovery(publishing: Publishing) -> None:
    """Recover a failed PR creation without another commit or duplicate PR."""
    config, record, root, state = publishing
    state_change(state, create_failure=True)
    failed = publish_run(config, record["run_id"], confirmed=True)
    assert failed["publication"]["failure_phase"] == "pull_request"
    commit = failed["publication"]["commit_sha"]
    state_change(state, create_failure=False)
    for _ in range(2):
        result = publish_run(config, record["run_id"], confirmed=True)
        assert result["publication"]["status"] == "published"
        assert result["publication"]["commit_sha"] == commit
    s = json.loads(state.read_text())
    assert s["pushes"] == 1
    assert (
        len([c for c in s["calls"] if c[:2] == ["pr", "create"]]) == 2
    )  # One failure, one success.


def test_push_failure_keeps_commit_and_resumes_without_duplicate_commit(
    publishing: Publishing,
) -> None:
    """Retry publication from the retained commit after a failed push."""
    config, record, root, state = publishing
    state_change(state, push_failure=True)
    failed = publish_run(config, record["run_id"], confirmed=True)
    assert failed["publication"]["failure_phase"] == "push"
    assert "DO_NOT_EXPORT" not in json.dumps(failed)
    commit = output(root, "rev-parse", "HEAD")
    state_change(state, push_failure=False)
    passed = publish_run(config, record["run_id"], confirmed=True)
    assert passed["publication"]["commit_sha"] == commit


@pytest.mark.parametrize(
    "problem",
    [
        "targeted_failed",
        "full_failed",
        "source_changed",
        "instructions",
        "historical",
        "scope",
        "baseline",
        "remote",
        "branch",
        "missing_tree",
        "staged",
        "secret",
        "selection",
        "no_permission",
        "unauthenticated",
        "unexpected_head",
    ],
)
def test_publication_gates(publishing: Publishing, problem: str) -> None:
    """Refuse publication when a required identity, evidence or safety gate fails."""
    config, record, root, state = publishing
    store = Store(config.runs_root)
    if problem == "targeted_failed":
        record["verification_attempts"][0]["status"] = "failed"
    elif problem == "full_failed":
        record["verification_attempts"][-1]["status"] = "failed"
    elif problem == "source_changed":
        (root / "hello.py").write_text("VALUE = 3\n")
    elif problem == "instructions":
        record["instruction_compliance"]["status"] = "failed"
    elif problem == "historical":
        record.pop("instruction_compliance")
    elif problem == "scope":
        record["approved_scope"] = [{"path": "unrelated", "kind": "directory"}]
    elif problem == "baseline":
        state_change(state, base="a" * 40)
    elif problem == "remote":
        git(root, "remote", "set-url", "origin", "https://github.com/wrong/project.git")
    elif problem == "branch":
        git(root, "checkout", "--detach")
    elif problem == "missing_tree":
        root.rename(root.with_name("moved"))
    elif problem == "staged":
        git(root, "add", "hello.py")
    elif problem == "secret":
        (root / ".env").write_text("PASSWORD=DO_NOT_EXPORT_123456\n")
    elif problem == "no_permission":
        state_change(state, no_permission=True)
    elif problem == "unauthenticated":
        state_change(state, unauthenticated=True)
    elif problem == "unexpected_head":
        git(root, "add", "hello.py")
        git(root, "commit", "-m", "Unexpected commit")
    store.save(record)
    options = {"files": ("hello.py",)} if problem == "selection" else {}
    with pytest.raises(DevAgentError):
        publish_run(config, record["run_id"], confirmed=True, **options)
    assert not json.loads(state.read_text()).get("pushes")


def test_incomplete_full_override_is_separate_and_prominent(publishing: Publishing) -> None:
    """Disclose the explicit standalone waiver without rewriting original check results."""
    config, record, root, state = publishing
    record["outcome"] = "verification_failed"
    record["verification_attempts"][-1].update(status="failed", outcome="verification_failed")
    Store(config.runs_root).save(record)
    result = publish_run(
        config,
        record["run_id"],
        confirmed=True,
        allow_incomplete_full=True,
        incomplete_full_reason="Disposable integration service unavailable; human review required.",
    )
    body = json.loads(state.read_text())["created_body"]
    assert "FULL REGRESSION VERIFICATION IS INCOMPLETE OR FAILED" in body
    assert "Targeted: passed" in body and "Full: failed" in body
    assert result["outcome"] == "verification_failed"


def test_dirty_source_and_ignored_files_preserved(publishing: Publishing) -> None:
    """Publish eligible retained changes without staging unrelated source or ignored files."""
    config, record, root, state = publishing
    source = config.project("example").repository
    (source / "hello.py").write_text("Unrelated dirty source\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "auth.json").write_text("private local data")
    before = status(source)
    result = publish_run(config, record["run_id"], confirmed=True)
    assert result["publication"]["status"] == "published"
    assert status(source) == before
    assert (root / "ignored" / "auth.json").read_text() == "private local data"
    assert "ignored/auth.json" not in output(root, "show", "--format=", "--name-only", "HEAD")


def test_force_added_ignored_file_cannot_enter_publication(publishing: Publishing) -> None:
    """Reject a force-added file even when its staged blob matches the supplied manifest."""
    import hashlib

    from dev_agent.delivery.publication import validate_staging

    config, record, root, state = publishing
    exclude = Path(
        output(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    )
    with exclude.open("a") as stream:
        stream.write("\nsrc/generated/\n")
    path = root / "src/generated/source.py"
    path.parent.mkdir(parents=True)
    content = b"ignored file under an allowlisted directory\n"
    path.write_bytes(content)
    git(root, "add", "-f", "src/generated/source.py")

    with pytest.raises(DevAgentError, match="Existing staged changes"):
        publish_run(config, record["run_id"], confirmed=True)
    manifest = [
        {
            "path": "src/generated/source.py",
            "change": "added",
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": path.stat().st_mode,
        }
    ]
    with pytest.raises(DevAgentError, match="New ignored paths"):
        validate_staging(root, manifest)
    assert not json.loads(state.read_text()).get("pushes")


def test_baseline_tracked_file_stays_publishable_when_ignored(publishing: Publishing) -> None:
    """Apply the ignored-addition guard without excluding existing tracked source."""
    config, record, root, _ = publishing
    exclude = Path(
        output(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    )
    with exclude.open("a") as stream:
        stream.write("\nhello.py\n")
    result = publish_run(config, record["run_id"], confirmed=True)
    assert result["publication"]["status"] == "published"
    files = result["publication"]["approved_plan"]["approved_files"]
    assert any(f["path"] == "hello.py" and f["change"] == "modified" for f in files)


@pytest.mark.parametrize(
    "ci,expected",
    [
        ("passed", "passed"),
        ("pending", "pending"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("skipped", "skipped"),
        ("absent", "no_checks_configured"),
    ],
)
def test_ci_status_and_history(publishing: Publishing, ci: str, expected: str) -> None:
    """Append exact-commit CI observations without replacing earlier results."""
    from dev_agent.delivery.publication import ci_run

    config, record, root, state = publishing
    result = publish_run(config, record["run_id"], confirmed=True)
    sha = result["publication"]["commit_sha"]
    state_change(state, checks=ci)
    observation = ci_run(config, record["run_id"])
    assert observation["status"] == expected and observation["commit_sha"] == sha
    saved = Store(config.runs_root).load(record["run_id"])
    assert len(saved["publication"]["ci_observations"]) == 2


def test_ci_timeout_and_commit_mismatch(publishing: Publishing) -> None:
    """Preserve monitoring timeout and changed-head outcomes as nonpasses."""
    from dev_agent.delivery.publication import ci_run

    config, record, root, state = publishing
    publish_run(config, record["run_id"], confirmed=True)
    state_change(state, checks="pending", workflows=True)
    assert ci_run(config, record["run_id"], watch=True)["status"] == "monitoring_timeout"
    data = json.loads(state.read_text())
    data["pull"]["head"]["sha"] = "b" * 40
    state.write_text(json.dumps(data))
    assert ci_run(config, record["run_id"])["status"] == "commit_mismatch"


def test_ci_retrieval_failure_and_no_checks_are_not_passes(publishing: Publishing) -> None:
    """Distinguish unavailable checks and absent checks from success."""
    from dev_agent.delivery.publication import ci_run

    config, record, root, state = publishing
    publish_run(config, record["run_id"], confirmed=True)
    state_change(state, checks_error=True)
    assert ci_run(config, record["run_id"])["status"] == "unable_to_retrieve"
    state_change(state, checks_error=False, checks="absent", workflows=True)
    assert ci_run(config, record["run_id"])["status"] == "no_checks"


def test_missing_gh_and_safe_remote_parser(
    publishing: Publishing, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse unavailable tooling and unsafe or unsupported GitHub destinations."""
    config, record, root, state = publishing
    monkeypatch.setattr("dev_agent.integrations.github.shutil.which", lambda *a, **k: None)
    with pytest.raises(GitHubError, match="missing"):
        GitHub(root, "example/project")
    for url in [
        "https://token@github.com/example/project",
        "ssh://evil/example/project",
        "https://github.com.evil/example/project",
        "/tmp/repo",
    ]:
        with pytest.raises(DevAgentError):
            remote_repository(url)
    assert remote_repository("git@github.com:example/project.git") == "example/project"


def test_cli_draft_confirmation_and_preview(
    publishing: Publishing, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enforce the preview and confirmation boundary through the CLI."""
    config, record, root, state = publishing
    assert (
        main(["--config", str(config.path), "publish", record["run_id"], "--draft", "--dry-run"])
        == 0
    )
    assert not json.loads(state.read_text()).get("pushes")
    assert (
        main(["--config", str(config.path), "publish", record["run_id"], "--draft", "--confirm"])
        == 0
    )
    assert main(["--config", str(config.path), "ci", record["run_id"]]) == 0
    assert "DO_NOT_EXPORT" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "tasks/TASK.md",
        "results/report.json",
        "src/private.key",
        "src/settings.local.json",
        "src/.npmrc",
        "src/agent-output.md",
    ],
)
def test_denied_files_even_inside_owner_allowlist(tmp_path: Path, path: str) -> None:
    """Block sensitive artifacts even under a configured publishable directory."""
    from dev_agent.delivery.publication import excluded_reason

    candidate = tmp_path / path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("content")
    assert excluded_reason(path, tmp_path, (path,))


def test_secret_binary_symlink_and_path_detection(tmp_path: Path) -> None:
    """Exclude inappropriate contents and reject paths that escape the worktree."""
    from dev_agent.delivery.publication import excluded_reason

    candidate = tmp_path / "source.py"
    candidate.write_text("-----BEGIN PRIVATE KEY-----\nsynthetic\n")
    assert "secret" in excluded_reason("source.py", tmp_path, ("source.py",))
    candidate.write_bytes(b"\0binary")
    assert "binary" in excluded_reason("source.py", tmp_path, ("source.py",))
    candidate.unlink()
    candidate.symlink_to("/does/not/exist")
    assert "symlink" in excluded_reason("source.py", tmp_path, ("source.py",))
    with pytest.raises(DevAgentError):
        excluded_reason("../outside.py", tmp_path, ("source.py",))


def test_approval_hash_change_blocks_before_staging(publishing: Publishing) -> None:
    """Require a new approval when the reviewed plan digest no longer matches."""
    config, record, root, state = publishing
    before = source_state(root)
    with pytest.raises(DevAgentError, match="plan changed"):
        publish_run(config, record["run_id"], confirmed=True, plan_sha256="wrong")
    assert source_state(root)["fingerprint"] == before["fingerprint"]
    assert not json.loads(state.read_text()).get("pushes")


def test_deleted_file_publication_and_repeated_identity(publishing: Publishing) -> None:
    """Validate a deletion in the manifest and reuse the publication on repetition."""
    config, record, root, state = publishing
    (root / "hello.py").unlink()
    verify(config, record["run_id"], "targeted")
    verify(config, record["run_id"], "full")
    result = publish_run(config, record["run_id"], confirmed=True)
    assert result["publication"]["status"] == "published"
    assert any(
        f["path"] == "hello.py" and f["change"] == "deleted"
        for f in result["publication"]["approved_plan"]["approved_files"]
    )
    assert (
        publish_run(config, record["run_id"], confirmed=True)["publication"]["status"]
        == "published"
    )


def test_source_modification_after_publication_is_rejected(publishing: Publishing) -> None:
    """Prevent existing approval from covering a changed retained candidate."""
    config, record, root, state = publishing
    publish_run(config, record["run_id"], confirmed=True)
    (root / "hello.py").write_text("VALUE = 99\n")
    with pytest.raises(DevAgentError, match="Source changed after publication"):
        publish_run(config, record["run_id"], confirmed=True)


@pytest.mark.parametrize("change", ["closed", "ready", "wrong_base", "wrong_head"])
def test_existing_incompatible_pr_blocks_republication(publishing: Publishing, change: str) -> None:
    """Refuse an existing PR with incompatible state or branch identity."""
    config, record, root, state = publishing
    publish_run(config, record["run_id"], confirmed=True)
    data = json.loads(state.read_text())
    if change == "closed":
        data["pull"]["state"] = "closed"
    if change == "ready":
        data["pull"]["draft"] = False
    if change == "wrong_base":
        data["pull"]["base"]["ref"] = "other"
    if change == "wrong_head":
        data["pull"]["head"]["sha"] = "a" * 40
    state.write_text(json.dumps(data))
    with pytest.raises(DevAgentError, match="Existing PR"):
        publish_run(config, record["run_id"], confirmed=True)


def test_explicit_stale_base_acknowledgement(publishing: Publishing) -> None:
    """Require operator acknowledgement of a stale remote base without rebasing."""
    config, record, root, state = publishing
    state_change(state, base="a" * 40)
    result = publish_run(config, record["run_id"], confirmed=True, acknowledge_stale_base=True)
    assert result["publication"]["status"] == "published"
    assert result["publication"]["approved_plan"]["stale_baseline"] is True
    assert output(root, "rev-parse", "HEAD^") == record["baseline_commit"]


@pytest.mark.parametrize(
    "fragment",
    [
        'repository = "https://github.com/example/project"\nallowed_paths=["src"]',
        'repository = "example/project"\nallowed_paths=["."]',
        'repository = "example/project"\nallowed_paths=["../src"]',
        'repository = "example/project"\nallowed_paths=["src/*"]',
        'repository = "example/project"\nallowed_paths=["src"]\nci_timeout_seconds=0',
    ],
)
def test_github_configuration_rejects_unsafe_settings(config_file: Path, fragment: str) -> None:
    """Reject invalid GitHub destinations, path allowlists or monitoring limits."""
    config_file.write_text(config_file.read_text() + "\n[projects.example.github]\n" + fragment)
    with pytest.raises(DevAgentError):
        load_config(config_file)
