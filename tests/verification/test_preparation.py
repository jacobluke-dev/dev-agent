"""Check trusted setup ordering, prerequisites, subprocess environment and recorded failures."""

import json
from pathlib import Path

import pytest

from dev_agent.config import Config, Prerequisites, load_config
from dev_agent.delivery.implementation import implement
from dev_agent.errors import DevAgentError
from dev_agent.process import ProcessResult
from dev_agent.records import Store
from tests.support.runs import configured, python_check, runner


def test_preparation_order_success_and_prerequisites(config: Config, prompt: Path) -> None:
    """Run setup commands in order and check generated prerequisites before starting the agent."""
    commands = (
        python_check(
            "first",
            "from pathlib import Path; Path('generated').mkdir(); "
            "Path('generated/order').write_text('1')",
        ),
        python_check(
            "second",
            "from pathlib import Path; p=Path('order'); assert p.read_text()=='1'; "
            "p.write_text('12'); print('prepared')",
            cwd="generated",
        ),
    )
    config = configured(
        config, preparation=commands, prerequisites=Prerequisites(paths=("generated/order",))
    )
    agent = runner()

    def implemented(worktree: Path, *args: object) -> ProcessResult:
        """Allow the fake agent only after the final prepared fixture exists."""
        assert (worktree / "generated/order").read_text() == "12"
        return ProcessResult(0, 0.1)

    agent.run.side_effect = implemented
    record = implement(config, "example", "TASK-123", prompt, runner=agent)
    assert record["outcome"] == "passed"
    assert record["preparation"]["status"] == "passed"
    assert record["prerequisites"]["status"] == "passed"
    first, second = record["preparation"]["commands"]
    assert first["finished_at"] <= second["started_at"]
    assert second["exit_code"] == 0 and second["duration_seconds"] >= 0
    assert Path(second["stdout_log"]).read_text() == "prepared\n"
    assert Path(second["stderr_log"]).read_text() == ""


@pytest.mark.parametrize("required", [True, False])
def test_required_and_optional_preparation_failure(
    config: Config, prompt: Path, required: bool
) -> None:
    """Stop on required setup failure and retain optional failure as a warning."""
    config = configured(
        config,
        preparation=(
            python_check("fails", "import sys; print('failure'); sys.exit(7)", required=required),
            python_check("later", "print('later')"),
        ),
    )
    agent = runner()
    record = implement(config, "example", "TASK-1", prompt, runner=agent)
    assert record["preparation"]["commands"][0]["exit_code"] == 7
    assert Path(record["worktree"]).is_dir()
    assert (Path(record["results_directory"]) / "summary.md").is_file()
    if required:
        assert record["outcome"] == "environment_failed"
        assert record["preparation"]["status"] == "failed"
        assert record["verification_status"] == "not_run"
        assert record["preparation"]["commands"][1]["status"] == "not_run"
        agent.run.assert_not_called()
    else:
        assert record["outcome"] == "passed_with_warnings"
        assert record["preparation"]["status"] == "optional_failed"
        assert record["verification_status"] == "passed"
        assert record["preparation"]["commands"][1]["status"] == "passed"
        agent.run.assert_called_once()


def test_preparation_timeout_no_agent(config: Config, prompt: Path) -> None:
    """Report setup timeout without launching the implementation agent."""
    config = configured(
        config,
        preparation=(python_check("slow", "import time; time.sleep(30)", timeout_seconds=1),),
    )
    agent = runner()
    record = implement(config, "example", "TASK-1", prompt, runner=agent)
    assert record["outcome"] == "environment_failed"
    result = record["preparation"]["commands"][0]
    assert result["timed_out"] and result["failure_kind"] == "timeout"
    assert result["exit_code"] < 0
    agent.run.assert_not_called()


@pytest.mark.parametrize("prerequisite", ["env", "path", "executable", "cwd", "check_env"])
def test_missing_prerequisites_before_codex(
    config: Config, prompt: Path, prerequisite: str
) -> None:
    """Block execution when a required host or command prerequisite is missing."""
    settings = {}
    if prerequisite == "env":
        settings["prerequisites"] = Prerequisites(env=("TEST_NONEXISTENT",))
    elif prerequisite == "path":
        settings["prerequisites"] = Prerequisites(paths=("missing/fixture",))
    elif prerequisite == "executable":
        settings["prerequisites"] = Prerequisites(executables=("dev-agent-nonexistent-command",))
    else:
        settings["verification"] = (
            python_check(
                "test",
                "print('never')",
                **(
                    {"cwd": "missing"}
                    if prerequisite == "cwd"
                    else {"required_env": ("TEST_NONEXISTENT",)}
                ),
            ),
        )
    config = configured(config, **settings)
    agent = runner()
    record = implement(config, "example", "TASK-1", prompt, runner=agent)
    assert record["outcome"] == "environment_failed"
    assert record["prerequisites"]["failures"]
    assert record["verification_attempts"] == []
    agent.run.assert_not_called()


def test_explicit_environment_scoped_and_not_recorded(
    config: Config, prompt: Path, fake_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expose variables to trusted checks only and keep their values out of run metadata."""
    monkeypatch.setenv("TEST_SYNTHETIC_SECRET", "private-inherited-value")
    monkeypatch.setenv("PRODUCTION_PASSWORD", "must-not-propagate")
    fake_codex.write_text(
        fake_codex.read_text() + "\nimport os\nassert 'TEST_SYNTHETIC_SECRET' not in os.environ\n"
        "assert 'SCOPED' not in os.environ\nassert 'PRODUCTION_PASSWORD' not in os.environ\n"
    )
    command = python_check(
        "environment",
        "import os; from pathlib import Path; assert os.getenv('SCOPED'); "
        "assert os.getenv('TEST_SYNTHETIC_SECRET'); "
        "assert 'PRODUCTION_PASSWORD' not in os.environ; "
        "assert Path(os.environ['PYTHONPATH']) == Path.cwd() / 'src'",
        env={"SCOPED": "configured-value"},
    )
    config = configured(
        config,
        env={"PYTHONPATH": "{worktree}/src"},
        inherit_env=("TEST_SYNTHETIC_SECRET",),
        preparation=(command,),
        verification=(command,),
    )
    record = implement(config, "example", "TASK-1", prompt)
    assert record["outcome"] == "passed"
    for filename in (
        config.runs_root / record["run_id"] / "record.json",
        Path(record["results_directory"]) / "report.json",
        Path(record["results_directory"]) / "summary.md",
    ):
        text = filename.read_text()
        assert "private-inherited-value" not in text
        assert "configured-value" not in text
    assert record["environment_names"] == ["PYTHONPATH"]


@pytest.mark.parametrize(
    "cwd", ["/tmp", "../escape", "a/../../escape", "C:\\outside", "a\\b", "~/source"]
)
def test_invalid_config_cwd_rejected(config_file: Path, cwd: str) -> None:
    """Refuse configured command working directories that escape the worktree."""
    config_file.write_text(
        config_file.read_text()
        + f"""\n[[projects.example.preparation]]
name = "setup"
argv = ["true"]
cwd = {json.dumps(cwd)}
"""
    )
    with pytest.raises(DevAgentError, match="relative"):
        load_config(config_file)


def test_runtime_symlink_cwd_refused(config: Config, prompt: Path, tmp_path: Path) -> None:
    """Refuse a command directory that becomes a symlink escape at runtime."""
    external = tmp_path / "external"
    external.mkdir()
    commands = (
        python_check(
            "link",
            "from pathlib import Path; "
            f"Path('escape').symlink_to({str(external)!r}, target_is_directory=True)",
        ),
        python_check("unsafe", "raise AssertionError('must not run')", cwd="escape"),
    )
    config = configured(config, preparation=commands)
    agent = runner()
    record = implement(config, "example", "TASK-1", prompt, runner=agent)
    assert record["outcome"] == "environment_failed"
    assert "escapes" in record["preparation"]["commands"][1]["error"]
    assert list(external.iterdir()) == []
    agent.run.assert_not_called()


def test_dry_run_includes_setup_without_execution(config: Config, prompt: Path) -> None:
    """Preview setup and checks while leaving their commands unexecuted."""
    config = configured(
        config, preparation=(python_check("setup", "raise AssertionError('never')"),)
    )
    agent = runner()
    record = implement(config, "example", "TASK-1", prompt, runner=agent, dry_run=True)
    assert record["preparation"]["commands"][0]["status"] == "not_run"
    assert not config.results_root.exists()
    assert not config.runs_root.exists()
    agent.preflight.assert_not_called()


def test_new_schema_parses_without_breaking_legacy(config_file: Path) -> None:
    """Support explicit preparation and prerequisites alongside legacy verification."""
    config_file.write_text(
        config_file.read_text()
        + """
[projects.example.env]
PYTHONPATH = "{worktree}/src"
[projects.example.prerequisites]
env = ["TEST_ONLY"]
paths = ["generated/fixture"]
executables = ["uv"]
[[projects.example.preparation]]
name = "python"
argv = ["uv", "sync", "--locked"]
cwd = "."
timeout_seconds = 120
required = false
inherit_env = ["TEST_ONLY"]
required_paths = ["pyproject.toml"]
[projects.example.preparation.env]
TEST_MODE = "synthetic"
[[projects.example.verification_profiles.targeted]]
name = "unit"
argv = ["uv", "run", "pytest", "tests/unit"]
[[projects.example.verification_profiles.full]]
name = "all"
argv = ["uv", "run", "pytest"]
"""
    )
    config = load_config(config_file)
    project = config.project("example")
    assert project.checks(None)[0] == "targeted"
    assert project.checks("full")[1][0].name == "all"
    assert project.checks("legacy")[1][0].name == "check"
    assert project.preparation[0].required is False
    assert project.preparation[0].env == {"TEST_MODE": "synthetic"}
    assert project.prerequisites.env == ("TEST_ONLY",)
    assert not config.results_root.exists()


@pytest.mark.parametrize(
    "addition",
    [
        "[projects.example.env]\nBAD = 7",
        '[[projects.example.preparation]]\nname="x"\nargv=["true"]\nrequired="yes"',
        '[[projects.example.verification_profiles.targeted]]\nname="x"\nargv=["true"]\nrequired=false',
        "[projects.example.verification_profiles]\nunknown=[]",
        '[projects.example.prerequisites]\npaths=["../unsafe"]',
        '[projects.example.prerequisites]\nenv=["BAD-NAME"]',
    ],
)
def test_invalid_new_config_rejected(config_file: Path, addition: str) -> None:
    """Reject invalid trusted setup and prerequisite declarations."""
    config_file.write_text(config_file.read_text() + "\n" + addition)
    with pytest.raises(DevAgentError):
        load_config(config_file)


def test_schema_v2_record(config: Config, prompt: Path) -> None:
    """Write the current implementation record schema for a new run."""
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    assert record["schema_version"] == 2
    assert Store(config.runs_root).load(record["run_id"]) == record


def test_configuration_provenance_uses_loaded_snapshot(
    config: Config, config_file: Path, prompt: Path
) -> None:
    """Keep the digest of the parsed settings even if the configuration file changes later."""
    import hashlib

    loaded_digest = hashlib.sha256(config_file.read_bytes()).hexdigest()
    # The already-loaded commands must retain their provenance if an editor changes the file.
    config_file.write_text("changed after configuration was loaded")
    record = implement(config, "example", "TASK-1", prompt, runner=runner())
    assert record["outcome"] == "passed"
    assert record["configuration_sha256"] == loaded_digest
    assert record["verification_attempts"][0]["configuration_sha256"] == loaded_digest


def test_cached_preparation_reuses_success_and_refreshes_changed_inputs(
    config: Config, repo: Path, prompt: Path
) -> None:
    """Retain a prepared environment until declared lockfile or installed file evidence changes."""
    from dev_agent.config import PreparationCache
    from dev_agent.repository.git import git
    from dev_agent.verification.runs import verify

    (repo / "toolchain.lock").write_text("v1")
    git(repo, "add", "toolchain.lock")
    git(repo, "commit", "-m", "Synthetic toolchain lockfile")
    config = configured(
        config,
        preparation=(
            python_check(
                "install",
                "from pathlib import Path; p=Path('ignored/installed'); "
                "p.parent.mkdir(exist_ok=True); "
                "p.write_text(p.read_text()+'1' if p.exists() else '1')",
            ),
        ),
        preparation_cache=PreparationCache(("toolchain.lock",), ("ignored/installed",)),
    )
    record = implement(config, "example", "TASK-123", prompt, runner=runner())
    root = Path(record["worktree"])
    marker = root / "ignored/installed"
    run_id = record["run_id"]
    assert record["verification_attempts"][-1]["preparation"]["status"] == "reused"
    again = verify(config, run_id, automatic_preparation=True)
    assert again["verification_attempts"][-1]["preparation"]["reuse"] == "validated_cache"
    assert marker.read_text() == "1"

    (root / "toolchain.lock").write_text("v2")
    again = verify(config, run_id, automatic_preparation=True)
    assert again["verification_attempts"][-1]["preparation"]["status"] == "passed"
    assert marker.read_text() == "11"
    marker.unlink()
    again = verify(config, run_id, automatic_preparation=True)
    assert again["verification_attempts"][-1]["preparation"]["status"] == "passed"
    assert marker.read_text() == "1"
    forced = verify(config, run_id, prepare_environment=True)
    assert forced["verification_attempts"][-1]["preparation"]["status"] == "passed"
    assert marker.read_text() == "11"


def test_lockfile_change_during_implementation_refreshes_before_initial_verification(
    config: Config, repo: Path, prompt: Path
) -> None:
    """Prepare the edited dependency graph before the agent's candidate is checked."""
    from dev_agent.config import PreparationCache
    from dev_agent.repository.git import git

    (repo / "toolchain.lock").write_text("before")
    git(repo, "add", "toolchain.lock")
    git(repo, "commit", "-m", "Synthetic dependency input")
    config = configured(
        config,
        preparation=(
            python_check(
                "install",
                "from pathlib import Path; p=Path('ignored/output'); "
                "p.parent.mkdir(exist_ok=True); p.write_text(Path('toolchain.lock').read_text())",
            ),
        ),
        preparation_cache=PreparationCache(("toolchain.lock",), ("ignored/output",)),
        verification=(
            python_check(
                "check",
                "from pathlib import Path; assert Path('ignored/output').read_text() == 'after'",
            ),
        ),
    )
    agent = runner()

    def change_input(root: Path, *args: object) -> ProcessResult:
        """Change the fixture lockfile only after the first setup has run."""
        assert (root / "ignored/output").read_text() == "before"
        (root / "toolchain.lock").write_text("after")
        return ProcessResult(0, 0.01)

    agent.run.side_effect = change_input
    run = implement(config, "example", "TASK-123", prompt, runner=agent)
    assert run["outcome"] == "passed"
    assert run["verification_attempts"][0]["preparation"]["status"] == "passed"
    assert (
        run["verification_attempts"][0]["preparation"]["fingerprint"]
        != run["preparation"]["fingerprint"]
    )


def test_preparation_cache_config_is_operator_defined(config_file: Path) -> None:
    """Parse literal preparation inputs and outputs only from the selected project settings."""
    original = config_file.read_text()
    config_file.write_text(
        original + '\n[projects.example.preparation_cache]\ninputs=["pkg.lock"]\n'
        'outputs=["generated/ready"]\n'
    )
    cache = load_config(config_file).project("example").preparation_cache
    assert cache.inputs == ("pkg.lock",) and cache.outputs == ("generated/ready",)
    config_file.write_text(config_file.read_text().replace('"pkg.lock"', '"../pkg.lock"'))
    with pytest.raises(DevAgentError, match="preparation_cache.inputs"):
        load_config(config_file)
