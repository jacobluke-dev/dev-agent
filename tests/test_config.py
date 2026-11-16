from pathlib import Path

import pytest

from dev_agent.config import identifier, load_config
from dev_agent.errors import DevAgentError


def test_config_loads_typed_defaults_and_commands(config_file: Path) -> None:
    config = load_config(config_file)
    project = config.project("example")
    assert project.base_branch == "main"
    assert project.verification[0].argv[-1] == "print('verified')"
    assert project.agent.model == "fake-model"
    assert project.agent.reasoning_effort == "medium"
    with pytest.raises(DevAgentError, match="Unknown project"):
        config.project("unknown")


@pytest.mark.parametrize("value", ["../escape", "a/b", "-arg", "", ".", "a b", "λ", "x" * 65])
def test_identifiers_prevent_traversal_and_options(value: str) -> None:
    with pytest.raises(DevAgentError):
        identifier(value)


@pytest.mark.parametrize(
    "before,after",
    [
        ('reasoning_effort = "medium"', 'reasoning_effort = "invalid"'),
        ("timeout_seconds = 10", "timeout_seconds = 0"),
        ("timeout_seconds = 10", "timeout_seconds = true"),
        ("timeout_seconds = 10", "timeout_seconds = 2.5"),
        ('model = "fake-model"', 'model = ""'),
        ('model = "fake-model"', "network_access = true"),
        ('base_branch = "main"', 'base_branch = "--help"'),
        ("argv = [", 'argv = "shell command" # ['),
        ('name = "check"', 'name = "../check"'),
        ("[projects.example]", '[projects."../bad"]'),
    ],
)
def test_invalid_config_rejected(config_file: Path, before: str, after: str) -> None:
    config_file.write_text(config_file.read_text().replace(before, after))
    with pytest.raises(DevAgentError):
        load_config(config_file)


def test_relative_paths_and_optional_agent(tmp_path: Path) -> None:
    filename = tmp_path / "config.toml"
    filename.write_text("""runs_root = "records"
[projects.one]
repository = "source"
base_branch = "main"
worktree_root = "trees"
[projects.two]
repository = "source-two"
base_branch = "dev"
worktree_root = "trees-two"
""")
    config = load_config(filename)
    assert config.runs_root == tmp_path / "records"
    assert config.project("one").repository == tmp_path / "source"
    assert config.project("one").verification == ()
    assert config.project("two").agent.timeout_seconds == 1800


@pytest.mark.parametrize("setting", ["runs_root", "worktree_root"])
def test_storage_inside_source_rejected(config_file: Path, repo: Path, setting: str) -> None:
    content = config_file.read_text().splitlines()
    content = [
        f'{setting} = "{repo}/storage"' if line.startswith(f"{setting} =") else line
        for line in content
    ]
    config_file.write_text("\n".join(content))
    with pytest.raises(DevAgentError, match="outside"):
        load_config(config_file)


def test_storage_overlap_rejected(config_file: Path, tmp_path: Path) -> None:
    config_file.write_text(
        config_file.read_text().replace(str(tmp_path / "trees"), str(tmp_path / "records/trees"))
    )
    with pytest.raises(DevAgentError, match="overlap"):
        load_config(config_file)


def test_storage_in_another_repo_rejected(config_file: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / ".git/HEAD").write_text("ref: refs/heads/main\n")
    config_file.write_text(
        config_file.read_text().replace(str(tmp_path / "records"), str(other / "records"))
    )
    with pytest.raises(DevAgentError, match="inside a Git"):
        load_config(config_file)


def test_missing_or_malformed_config(tmp_path: Path) -> None:
    file = tmp_path / "missing.toml"
    with pytest.raises(DevAgentError, match="Cannot read"):
        load_config(file)
    for content in ("x=[", "", "projects = []", "unknown = 2"):
        file.write_text(content)
        with pytest.raises(DevAgentError):
            load_config(file)


def test_duplicate_checks_rejected(config_file: Path) -> None:
    config_file.write_text(
        config_file.read_text()
        + """
[[projects.example.verification]]
name = "check"
argv = ["true"]
"""
    )
    with pytest.raises(DevAgentError, match="Duplicate"):
        load_config(config_file)
