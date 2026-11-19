"""Protect baseline instruction discovery, supplied provenance and enforcement in both workflows."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from dev_agent.cli import exit_code, main
from dev_agent.config import Check, Config, RepositoryContext, load_config
from dev_agent.delivery.implementation import implement
from dev_agent.errors import DevAgentError
from dev_agent.investigation.workflow import investigate
from dev_agent.records import Store
from dev_agent.repository.git import git, output, status
from dev_agent.repository.instructions import (
    InstructionError,
    applicable_sets,
    approved_scopes,
    audit_files,
    check_documents,
    resolve,
    supplied,
    validate_changes,
)
from dev_agent.repository.snapshot import ContextError
from dev_agent.run_admin import inspect_run
from tests.support.atlassian import atlassian as atlassian
from tests.support.investigation import (
    FakeInvestigator,
    investigation_config,
)
from tests.support.investigation import (
    investigator as investigator,
)


@pytest.fixture
def hierarchy(repo: Path) -> Path:
    """Commit fixture documents and source paths with multiple governed directory scopes."""
    files = {
        "frontend/AGENTS.md": "FRONTEND: preserve component contracts.\n",
        "src/example_service/AGENTS.md": "BACKEND: preserve API contracts.\n",
        "tests/AGENTS.md": "TESTS: use deterministic fixtures.\n",
        "frontend/components/Button.tsx": "export const label = 'Button';\n",
        "src/example_service/api/models.py": "VALUE = 1\n",
        "tests/unit_tests/example.py": "assert True\n",
        "docs/architecture.md": "Architecture: shared contracts are authoritative.\n",
    }
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic sibling instruction hierarchy")
    return repo


def bundle_for(repo: Path, policy: RepositoryContext | None = None):
    """Resolve a fixture's exact HEAD with the supplied or default repository policy."""
    return resolve(repo, output(repo, "rev-parse", "main"), policy or RepositoryContext())


@pytest.mark.parametrize(
    "path,expected",
    [
        ("frontend/components/Button.tsx", ["AGENTS.md", "frontend/AGENTS.md"]),
        (
            "src/example_service/api/models.py",
            ["AGENTS.md", "src/example_service/AGENTS.md"],
        ),
        ("tests/unit_tests/example.py", ["AGENTS.md", "tests/AGENTS.md"]),
        ("new/untracked/module.py", ["AGENTS.md"]),
    ],
)
def test_synthetic_sibling_applicability(hierarchy: Path, path: str, expected: list[str]) -> None:
    """Apply only the selected path's ancestor instructions across sibling directories."""
    bundle = bundle_for(hierarchy)
    assert bundle.applicable(path) == expected
    assert len([d for d in bundle.documents if d["kind"] == "instruction"]) == 4
    assert bundle.applicable("frontend", directory=True) == ["AGENTS.md", "frontend/AGENTS.md"]


def test_multi_area_union_and_manifest_provenance(hierarchy: Path) -> None:
    """Keep a complete bounded manifest while recording independent per-path chains."""
    bundle = bundle_for(hierarchy)
    state = bundle.provenance()
    assert state["status"] == "resolved" and not any(d["supplied"] for d in state["documents"])
    supplied(bundle, state, bundle.render())
    paths = ["src/example_service/api/models.py", "tests/unit_tests/example.py"]
    mapping = applicable_sets(bundle, state, paths)
    assert set(sum(mapping.values(), [])) == {
        "AGENTS.md",
        "src/example_service/AGENTS.md",
        "tests/AGENTS.md",
    }
    assert "frontend/AGENTS.md" not in sum(mapping.values(), [])
    for document in state["documents"]:
        assert document["commit"] == bundle.commit
        assert document["required"] and document["supplied"]
        assert (
            document["content_sha256"]
            == hashlib.sha256((hierarchy / document["path"]).read_bytes()).hexdigest()
        )
        assert document["available_source_location"] and document["source_location"]
        assert "content" not in document


def test_arbitrary_depth_parent_child_precedence(repo: Path) -> None:
    """Keep all applicable nested instructions in root-to-leaf order."""
    names = ["AGENTS.md"]
    for depth in range(1, 45):
        folder = repo.joinpath(*(["layer"] * depth))
        folder.mkdir(exist_ok=True)
        (folder / "AGENTS.md").write_text(f"Depth {depth} engineering conventions.\n")
        names.append((folder / "AGENTS.md").relative_to(repo).as_posix())
    git(repo, "add", ".")
    git(repo, "commit", "-m", "deep instructions")
    bundle = bundle_for(repo)
    assert bundle.applicable("/".join(["layer"] * 44 + ["new.py"])) == names


@pytest.mark.parametrize("path", ["AGENTS.md", "frontend/AGENTS.md"])
@pytest.mark.parametrize(
    "bad", ["missing", "empty", "whitespace", "nul", "invalid_utf8", "control", "symlink"]
)
def test_mandatory_document_validation(
    hierarchy: Path, tmp_path: Path, path: str, bad: str
) -> None:
    """Reject unsafe, missing or malformed mandatory baseline documents."""
    document = hierarchy / path
    document.unlink()
    content = {
        "empty": b"",
        "whitespace": b"  \n",
        "nul": b"rules\x00",
        "invalid_utf8": b"\xff",
        "control": b"\x1b[31mrules",
    }
    if bad == "symlink":
        target = tmp_path / "private"
        target.write_text("not to be read")
        document.symlink_to(target)
    elif bad != "missing":
        document.write_bytes(content[bad])
    git(hierarchy, "add", "-A")
    git(hierarchy, "commit", "-m", "invalid synthetic instructions")
    with pytest.raises(InstructionError) as error:
        # A deleted nested file is no longer discoverable in that baseline. An explicit
        # required reference verifies the missing-document gate; otherwise root is inherited.
        policy = RepositoryContext(instructions=(path,)) if bad == "missing" else None
        bundle_for(hierarchy, policy)
    assert error.value.compliance["status"] == "failed"
    assert any(d["path"] == path and d["status"] == "unavailable" for d in error.value.documents)


@pytest.mark.parametrize("limits", [dict(max_document_bytes=1), dict(max_total_bytes=45)])
def test_oversize_instructions_fail_without_truncation(hierarchy: Path, limits: dict) -> None:
    """Fail closed when a mandatory instruction exceeds configured bounds."""
    with pytest.raises(InstructionError, match="limit exceeded.*truncated"):
        bundle_for(hierarchy, RepositoryContext(**limits))


@pytest.mark.parametrize(
    "path",
    ["../AGENTS.md", "/AGENTS.md", "a/../b", "C:/secret", "a\\b", "./hello.py", "a//b", "a\n/file"],
)
def test_unsafe_resolution_paths(hierarchy: Path, path: str) -> None:
    """Reject paths that cannot identify a safe canonical repository location."""
    bundle = bundle_for(hierarchy)
    with pytest.raises(DevAgentError):
        bundle.applicable(path)
    with pytest.raises(DevAgentError):
        bundle_for(hierarchy, RepositoryContext(required_documents=(path,)))


def test_baseline_excludes_dirty_and_other_branch_instructions(hierarchy: Path) -> None:
    """Read instructions from committed baseline objects independently of checkout contents."""
    before = output(hierarchy, "rev-parse", "main")
    git(hierarchy, "switch", "-c", "unrelated")
    (hierarchy / "feature").mkdir()
    (hierarchy / "feature/AGENTS.md").write_text("Unmerged rules.\n")
    git(hierarchy, "add", ".")
    git(hierarchy, "commit", "-m", "unmerged synthetic instructions")
    (hierarchy / "AGENTS.md").write_text("DIRTY root must not be supplied.\n")
    original = status(hierarchy, ignored=True)
    bundle = bundle_for(hierarchy)
    assert bundle.commit == before
    assert "DIRTY" not in bundle.render() and "Unmerged rules" not in bundle.render()
    assert bundle.applicable("feature/file.py") == ["AGENTS.md"]
    assert status(hierarchy, ignored=True) == original
    assert output(hierarchy, "branch", "--show-current") == "unrelated"


def test_required_architecture_and_legacy_migration(hierarchy: Path) -> None:
    """Preserve separately configured architecture evidence and compatible aliases."""
    policy = RepositoryContext(
        required_documents=("docs/architecture.md",), instructions=("AGENTS.md",)
    )
    bundle = bundle_for(hierarchy, policy)
    assert len(bundle.documents) == 5
    assert "shared contracts" in bundle.render()
    assert bundle.provenance()["migration_notes"]
    assert bundle.documents[-1]["kind"] == "architecture"
    with pytest.raises(InstructionError, match="missing.md"):
        bundle_for(hierarchy, RepositoryContext(required_documents=("docs/missing.md",)))
    with pytest.raises(InstructionError, match="cannot be optional"):
        bundle_for(hierarchy, RepositoryContext(optional_instructions=("frontend/AGENTS.md",)))


def test_missing_applicable_supply_and_provenance_tampering_fail(hierarchy: Path) -> None:
    """Reject instruction evidence if a required document was not supplied or its hash differs."""
    bundle = bundle_for(hierarchy)
    state = bundle.provenance()
    with pytest.raises(InstructionError, match="not supplied"):
        applicable_sets(bundle, state, ["frontend/file.py"])
    with pytest.raises(InstructionError, match="not supplied unchanged"):
        supplied(bundle, state, "Root guidance only")
    state["documents"].pop()
    with pytest.raises(InstructionError, match="provenance differs"):
        supplied(bundle, state, bundle.render())
    state = bundle.provenance()
    supplied(bundle, state, bundle.render())
    next(d for d in bundle.documents if d["path"] == "frontend/AGENTS.md").pop("content")
    with pytest.raises(InstructionError, match="unavailable"):
        bundle.applicable("frontend/file.py")


def test_invalid_unknown_and_literal_modification_scopes(hierarchy: Path) -> None:
    """Require known literal baseline file or directory scopes."""
    bundle = bundle_for(hierarchy)
    assert approved_scopes(bundle, ("frontend", "hello.py")) == [
        {"path": "frontend", "kind": "directory"},
        {"path": "hello.py", "kind": "file"},
    ]
    for values in ((), ("absent",), ("frontend/**",), ("../outside",), ("hello.py", "hello.py")):
        with pytest.raises(DevAgentError):
            approved_scopes(bundle, values)


def test_preparation_cannot_replace_mandatory_context(
    config: Config,
    prompt: Path,
) -> None:
    """Block the agent when preparation alters a required instruction document."""
    import sys

    project = replace(
        config.project("example"),
        preparation=(
            Check(
                "rewrite",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('AGENTS.md').write_text('Replacement rules')",
                ),
            ),
        ),
    )
    runner = Mock()
    runner.preflight.return_value = {}
    record = implement(
        replace(config, projects={"example": project}), "example", "TASK-1", prompt, runner=runner
    )
    assert record["outcome"] == "instructions_failed"
    assert record["instruction_compliance"]["status"] == "failed"
    runner.run.assert_not_called()
    assert Path(record["worktree"]).is_dir()


def test_implementation_complete_context_and_changed_scope_provenance(
    config: Config,
    hierarchy: Path,
    prompt: Path,
    fake_codex: Path,
) -> None:
    """Record the supplied context and applicable chains for accepted changes."""
    policy = replace(
        config.project("example").context, required_documents=("docs/architecture.md",)
    )
    config = replace(
        config, projects={"example": replace(config.project("example"), context=policy)}
    )
    fake_codex.write_text(
        fake_codex.read_text()
        + """
assert 'FRONTEND: preserve component contracts.' in prompt
assert 'BACKEND: preserve API contracts.' in prompt
assert 'TESTS: use deterministic fixtures.' in prompt
assert 'Architecture: shared contracts are authoritative.' in prompt
pathlib.Path('frontend/components/Button.tsx').write_text('export const label = "Updated";\\n')
pathlib.Path('src/example_service/api/models.py').write_text('VALUE = 2\\n')
pathlib.Path('tests/unit_tests/example.py').write_text('assert 1 == 1\\n')
"""
    )
    record = implement(config, "example", "TASK-123", prompt)
    assert record["outcome"] == "passed"
    state = record["instruction_compliance"]
    assert state["status"] == "changed_scopes_validated"
    assert all(d["supplied"] for d in state["documents"])
    for path in (
        "frontend/components/Button.tsx",
        "src/example_service/api/models.py",
        "tests/unit_tests/example.py",
    ):
        assert len(state["changed_file_validation"]["applicable_sets"][path]) == 2
    assert Path(record["results_directory"], "repository-instructions.json").is_file()
    assert "Instruction compliance" in Path(record["results_directory"], "summary.md").read_text()
    assert Store(config.runs_root).load(record["run_id"])["instruction_compliance"] == state
    assert inspect_run(config, record["run_id"])["instruction_compliance"] == state
    assert output(hierarchy, "branch", "--show-current") == "main"


def test_ignored_modification_cannot_bypass_approved_scope(
    config: Config,
    hierarchy: Path,
    prompt: Path,
    fake_codex: Path,
) -> None:
    """Include ignored files when checking agent modifications against approved scope."""
    code = (
        fake_codex.read_text()
        .replace("Path('hello.py').write_text", "Path('frontend/new.py').write_text")
        .replace("Path('new file.txt').write_text", "Path('frontend/new.txt').write_text")
    )
    fake_codex.write_text(
        code + "\npathlib.Path('ignored').mkdir()\n"
        "pathlib.Path('ignored/hidden.py').write_text('changed')\n"
    )
    record = implement(config, "example", "TASK-123", prompt, scope=("frontend",))
    assert record["outcome"] == "instructions_failed"
    assert record["verification_status"] == "not_run"
    assert record["agent"]["status"] == "passed"
    validation = record["instruction_compliance"]["changed_file_validation"]
    assert {
        "path": "ignored/hidden.py",
        "reason": "outside approved modification scope",
    } in validation["violations"]
    assert "ignored/hidden.py" not in record["files_changed"]
    assert exit_code(record["outcome"]) == 10


@pytest.mark.parametrize("action", ["create", "edit", "delete", "rename"])
def test_instruction_authority_changes_rejected(
    config: Config,
    hierarchy: Path,
    prompt: Path,
    fake_codex: Path,
    action: str,
) -> None:
    """Reject agent changes that modify or introduce instruction authority."""
    code = {
        "create": "pathlib.Path('new').mkdir(); "
        "pathlib.Path('new/AGENTS.md').write_text('New policy')",
        "edit": "pathlib.Path('frontend/AGENTS.md').write_text('Changed policy')",
        "delete": "pathlib.Path('frontend/AGENTS.md').unlink()",
        "rename": "pathlib.Path('frontend/AGENTS.md').rename('frontend/old.md')",
    }[action]
    fake_codex.write_text(fake_codex.read_text() + "\n" + code + "\n")
    record = implement(config, "example", "TASK-123", prompt)
    assert record["outcome"] == "instructions_failed"
    assert record["instruction_compliance"]["changed_file_validation"]["violations"]
    assert record["verification_status"] == "not_run"


def test_root_failure_and_unknown_scope_prevent_agent_and_worktree(
    config: Config,
    repo: Path,
    prompt: Path,
) -> None:
    """Check baseline instruction and scope validity before provider and worktree allocation."""
    runner = Mock()
    with pytest.raises(InstructionError, match="Unknown"):
        implement(config, "example", "TASK-1", prompt, runner=runner, scope=("unknown",))
    (repo / "AGENTS.md").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "missing root")
    for dry in (False, True):
        with pytest.raises(InstructionError, match="AGENTS.md"):
            implement(config, "example", "TASK-1", prompt, runner=runner, dry_run=dry)
    runner.preflight.assert_not_called()
    runner.run.assert_not_called()
    assert not config.project("example").worktree_root.exists()


@pytest.mark.parametrize("limit", ["max_audit_bytes", "max_audit_files", "max_prompt_bytes"])
def test_instruction_resource_limits_do_not_launch_agent(
    config: Config,
    prompt: Path,
    limit: str,
) -> None:
    """Stop before provider execution when complete mandatory context exceeds bounds."""
    runner = Mock()
    runner.preflight.return_value = {}
    policy = replace(config.project("example").context, **{limit: 1})
    cfg = replace(config, projects={"example": replace(config.project("example"), context=policy)})
    if limit == "max_prompt_bytes":
        with pytest.raises(InstructionError, match="max_prompt_bytes"):
            implement(cfg, "example", "TASK-1", prompt, runner=runner)
    else:
        result = implement(cfg, "example", "TASK-1", prompt, runner=runner)
        assert result["outcome"] == "instructions_failed"
    runner.run.assert_not_called()


def test_investigation_supplies_hierarchy_and_validates_cited_scopes(
    config: Config,
    hierarchy: Path,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Supply nested context and record the governing chains of cited source files."""

    def assessment(value: dict) -> dict:
        """Add valid fixture citations from separate governed directory scopes."""
        for path, quote in (
            ("frontend/components/Button.tsx", "export const label"),
            ("src/example_service/api/models.py", "VALUE = 1"),
            ("tests/unit_tests/example.py", "assert True"),
        ):
            value["criteria"][0]["evidence"].append(
                {
                    "path": path,
                    "line_start": 1,
                    "line_end": 1,
                    "symbol": "",
                    "quote": quote,
                    "kind": "static_implementation",
                }
            )
        return value

    investigator.output = assessment
    result = investigate(
        investigation_config(config, atlassian[0]),
        "example",
        "TASK-123",
        provider=atlassian[0],
        runner=investigator,
    )
    assert result["outcome"] == "investigated"
    state = result["instruction_compliance"]
    assert state["status"] == "cited_scopes_validated" and state["dynamic_interception"] is False
    assert state["applicable_sets"]["frontend/components/Button.tsx"] == [
        "AGENTS.md",
        "frontend/AGENTS.md",
    ]
    assert state["applicable_sets"]["tests/unit_tests/example.py"] == [
        "AGENTS.md",
        "tests/AGENTS.md",
    ]
    assert state["unverified_areas"]
    assert Path(result["results_directory"], "repository-instructions.json").exists()
    assert (
        "Instruction compliance"
        in Path(result["results_directory"], "investigation.md").read_text()
    )


def test_investigation_instructions_before_http_and_historical_lookup(
    config: Config,
    hierarchy: Path,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Protect required local instructions before HTTP and preserve legacy record inspection."""
    (hierarchy / "tests/AGENTS.md").write_text("")
    git(hierarchy, "add", ".")
    git(hierarchy, "commit", "-m", "invalid required test guidance")
    result = investigate(
        investigation_config(config, atlassian[0]),
        "example",
        "TASK-123",
        provider=atlassian[0],
        runner=investigator,
    )
    assert result["outcome"] == "context_failed" and not atlassian[1].calls
    assert result["instruction_compliance"]["status"] == "failed" and not investigator.called
    assert any(
        d["path"] == "tests/AGENTS.md" and d["status"] == "unavailable"
        for d in result["instruction_compliance"]["documents"]
    )
    old = result.copy()
    old.pop("instruction_compliance")
    Store(config.runs_root).save(old)
    assert inspect_run(config, old["run_id"])["instruction_compliance"] == {
        "status": "not_recorded"
    }


@pytest.mark.parametrize(
    "setting",
    [
        "require_root_instructions = false",
        "enforce_nested_instructions = false",
        'instruction_filename = "OTHER.md"',
        'optional_instructions = ["frontend/AGENTS.md"]',
        "max_audit_files = 0",
        "max_audit_bytes = true",
        "max_prompt_bytes = 0",
        'required_documents = ["../escape"]',
        "implementation_scope = []",
        'implementation_scope = ["frontend", "frontend"]',
    ],
)
def test_configuration_cannot_disable_mandatory_policy(config_file: Path, setting: str) -> None:
    """Refuse TOML settings that disable mandatory root or nested instruction handling."""
    config_file.write_text(config_file.read_text() + f"\n[projects.example.context]\n{setting}\n")
    with pytest.raises(DevAgentError):
        load_config(config_file)


def test_config_and_cli_scope_selection(
    config_file: Path,
    config: Config,
    hierarchy: Path,
    prompt: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Resolve default scopes and literal CLI selections through implementation planning."""
    config_file.write_text(
        config_file.read_text()
        + """
[projects.example.context]
require_root_instructions = true
enforce_nested_instructions = true
required_documents = ["docs/architecture.md"]
implementation_scope = ["frontend", "tests"]
"""
    )
    cfg = load_config(config_file)
    assert cfg.project("example").context.required_documents == ("docs/architecture.md",)
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
                "--scope",
                "hello.py",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    data = json.loads(text)["plan"]
    assert data["approved_scope"] == [{"path": "hello.py", "kind": "file"}]
    assert not config.runs_root.exists()


def test_broad_audit_and_missing_applicable_hash_gate(hierarchy: Path) -> None:
    """Reject an audit result when applicable supplied instruction evidence is incomplete."""
    bundle = bundle_for(hierarchy)
    state = bundle.provenance()
    supplied(bundle, state, bundle.render())
    before = audit_files(hierarchy, RepositoryContext())
    check_documents(bundle, before, worktree=hierarchy)
    file = hierarchy / "frontend/new.py"
    file.write_text("New implementation")
    after = audit_files(hierarchy, RepositoryContext())
    next(d for d in state["documents"] if d["path"] == "frontend/AGENTS.md")["supplied"] = False
    result = validate_changes(
        bundle, state, before, after, approved_scopes(bundle, (".",)), hierarchy
    )
    assert result["status"] == "failed" and "not supplied" in result["violations"][0]["reason"]


def test_nested_blob_read_failure_is_not_silently_ignored(
    hierarchy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail the instruction bundle when a discovered nested Git object cannot be read."""
    from dev_agent.repository import instructions

    original = instructions.git
    oid = output(hierarchy, "rev-parse", "main:frontend/AGENTS.md")

    def unreadable(repo: Path, *args: str, **kwargs: object):
        """Raise a fixture read failure only for the targeted instruction blob."""
        if args == ("cat-file", "blob", oid):
            raise DevAgentError("Synthetic unavailable Git blob")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(instructions, "git", unreadable)
    with pytest.raises(InstructionError, match="unreadable"):
        bundle_for(hierarchy)


def test_baseline_without_nested_document_inherits_root(hierarchy: Path) -> None:
    """Apply the root instructions when a directory has no nested governing document."""
    (hierarchy / "frontend/AGENTS.md").unlink()
    git(hierarchy, "add", "-A")
    git(hierarchy, "commit", "-m", "baseline without subtree-specific guide")
    assert bundle_for(hierarchy).applicable("frontend/components/Button.tsx") == ["AGENTS.md"]


def test_instruction_manifest_itself_is_bounded(hierarchy: Path) -> None:
    """Enforce audit limits even when the overhead is dominated by instruction metadata."""
    # Many tiny documents can exceed prompt capacity even if their bodies fit the byte limit.
    with pytest.raises(InstructionError, match="manifest exceeds"):
        bundle_for(hierarchy, RepositoryContext(max_prompt_bytes=1024))


def test_missing_root_not_falsely_reported_as_discovered(repo: Path) -> None:
    """Report a required missing root without recording it as discovered context."""
    (repo / "AGENTS.md").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "missing mandatory root")
    with pytest.raises(InstructionError) as exc:
        bundle_for(repo)
    document = exc.value.compliance["documents"][0]
    assert document["path"] == "AGENTS.md" and document["discovered"] is False
    assert document["supplied"] is False


def test_generated_instruction_scope_stops_implementation_before_agent(
    config: Config,
    prompt: Path,
) -> None:
    """Reject a newly generated instruction scope before handing the worktree to Codex."""
    import sys

    check = Check(
        "generated-policy",
        (
            sys.executable,
            "-c",
            "from pathlib import Path; Path('new').mkdir(); "
            "Path('new/AGENTS.md').write_text('New rules')",
        ),
    )
    project = replace(config.project("example"), preparation=(check,))
    runner = Mock()
    runner.preflight.return_value = {}
    result = implement(
        replace(config, projects={"example": project}), "example", "TASK-123", prompt, runner=runner
    )
    assert result["outcome"] == "instructions_failed"
    assert "absent from the baseline manifest" in result["error"]
    runner.run.assert_not_called()


def test_post_agent_audit_limit_rejects_zero_agent_exit(
    config: Config,
    prompt: Path,
    fake_codex: Path,
) -> None:
    """Reject accepted completion if the controller cannot finish the post-agent audit."""
    fake_codex.write_text(
        fake_codex.read_text() + "\npathlib.Path('large.txt').write_bytes(b'x' * 1025)\n"
    )
    policy = replace(config.project("example").context, max_audit_bytes=1024)
    project = replace(config.project("example"), context=policy)
    result = implement(
        replace(config, projects={"example": project}), "example", "TASK-123", prompt
    )
    assert result["outcome"] == "instructions_failed"
    assert result["agent"]["exit_code"] == 0
    assert result["verification_status"] == "not_run"
    assert result["instruction_compliance"]["status"] == "failed"


def test_credential_in_mandatory_investigation_context_fails_before_http(
    config: Config,
    repo: Path,
    atlassian: tuple,
    investigator: FakeInvestigator,
) -> None:
    """Keep known credential values in mandatory context out of requests and prompts."""
    import os

    secret = os.environ["DEV_AGENT_TEST_TOKEN"]
    (repo / "AGENTS.md").write_text("Unsafe embedded credential: " + secret)
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-m", "synthetic credential in guidance")
    result = investigate(
        investigation_config(config, atlassian[0]),
        "example",
        "TASK-123",
        provider=atlassian[0],
        runner=investigator,
    )
    assert result["outcome"] == "context_failed" and not atlassian[1].calls
    assert not investigator.called
    assert secret not in json.dumps(result)
    canonical = Store(config.runs_root).directory(result["run_id"])
    assert not (canonical / "repository-instructions.json").exists()


def test_audit_rejects_files_changing_during_capture(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject evidence for a file whose metadata changes during capture."""
    import stat

    from dev_agent.repository import instructions

    original = instructions.os.fstat
    calls = 0

    def changed(fd: int):
        """Simulate a modification timestamp change on the second descriptor inspection."""
        from types import SimpleNamespace

        nonlocal calls
        value = original(fd)
        if not stat.S_ISREG(value.st_mode):
            return value
        calls += 1
        if calls == 2:
            return SimpleNamespace(
                st_size=value.st_size,
                st_mtime_ns=value.st_mtime_ns + 1,
                st_ctime_ns=value.st_ctime_ns,
            )
        return value

    monkeypatch.setattr(instructions.os, "fstat", changed)
    with pytest.raises(ContextError, match="changed during capture"):
        audit_files(repo, RepositoryContext())


def test_empty_directories_cannot_bypass_audit_resource_limit(repo: Path) -> None:
    """Bound audit traversal even when a large directory tree contains no files."""
    for index in range(10):
        (repo / f"empty-{index}").mkdir()
    with pytest.raises(ContextError, match="max_audit_files"):
        audit_files(repo, RepositoryContext(max_audit_files=10))


@pytest.mark.parametrize("directory", [".codex", ".agents", ".aws", "nested/.codex"])
@pytest.mark.parametrize("select_file", [False, True])
def test_control_scopes_rejected_before_preflight_and_worktree(
    config: Config,
    repo: Path,
    prompt: Path,
    directory: str,
    select_file: bool,
) -> None:
    """Reject agent-control paths in approved scopes before execution allocation."""
    document = repo / directory / "synthetic-settings.txt"
    document.parent.mkdir(parents=True)
    document.write_text("Synthetic configuration; no credentials.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic tracked control directory")
    selected = document.relative_to(repo).as_posix() if select_file else directory
    runner = Mock()
    original_refs = output(repo, "show-ref")
    with pytest.raises(InstructionError, match="Unknown or unsafe implementation scope"):
        implement(config, "example", "TASK-123", prompt, scope=(selected,), runner=runner)
    runner.preflight.assert_not_called()
    runner.run.assert_not_called()
    assert output(repo, "show-ref") == original_refs
    assert not config.project("example").worktree_root.exists()
    assert not config.runs_root.exists()
