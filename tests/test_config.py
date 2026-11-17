"""Verify the strict TOML schema, compatibility defaults and path validation."""

from pathlib import Path

import pytest

from dev_agent.config import identifier, load_config
from dev_agent.errors import DevAgentError


def test_config_loads_typed_defaults_and_commands(config_file: Path) -> None:
    """Load typed external project settings and preserve configured command arguments."""
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
    """Refuse identifiers that can act as paths, options or unsafe argument values."""
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
    """Reject malformed values and unsupported settings at configuration load time."""
    config_file.write_text(config_file.read_text().replace(before, after))
    with pytest.raises(DevAgentError):
        load_config(config_file)


def test_relative_paths_and_optional_agent(tmp_path: Path) -> None:
    """Resolve paths relative to the selected TOML and retain optional agent defaults."""
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
    """Require canonical run and worktree storage to stay outside managed source."""
    content = config_file.read_text().splitlines()
    content = [
        f'{setting} = "{repo}/storage"' if line.startswith(f"{setting} =") else line
        for line in content
    ]
    config_file.write_text("\n".join(content))
    with pytest.raises(DevAgentError, match="outside"):
        load_config(config_file)


def test_storage_overlap_rejected(config_file: Path, tmp_path: Path) -> None:
    """Reject conflicting worktree and canonical storage paths."""
    config_file.write_text(
        config_file.read_text().replace(str(tmp_path / "trees"), str(tmp_path / "records/trees"))
    )
    with pytest.raises(DevAgentError, match="overlap"):
        load_config(config_file)


def test_storage_in_another_repo_rejected(config_file: Path, tmp_path: Path) -> None:
    """Keep run storage out of another Git repository."""
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / ".git/HEAD").write_text("ref: refs/heads/main\n")
    config_file.write_text(
        config_file.read_text().replace(str(tmp_path / "records"), str(other / "records"))
    )
    with pytest.raises(DevAgentError, match="inside a Git"):
        load_config(config_file)


def test_missing_or_malformed_config(tmp_path: Path) -> None:
    """Return a clear configuration error for absent or invalid TOML."""
    file = tmp_path / "missing.toml"
    with pytest.raises(DevAgentError, match="Cannot read"):
        load_config(file)
    for content in ("x=[", "", "projects = []", "unknown = 2"):
        file.write_text(content)
        with pytest.raises(DevAgentError):
            load_config(file)


def test_duplicate_checks_rejected(config_file: Path) -> None:
    """Keep command names unique so check evidence has unambiguous paths."""
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


def test_investigation_configuration_defaults(config_file: Path) -> None:
    """Load bounded investigation and provider defaults from minimal trusted settings."""
    config_file.write_text(
        config_file.read_text()
        + """
[projects.example.atlassian]
site_url = "https://example.atlassian.net/"
email_env = "TASK_EMAIL"
token_env = "TASK_TOKEN"
[projects.example.context]
instructions = ["AGENTS.md", "docs/architecture.md"]
optional_instructions = ["docs/testing.md"]
"""
    )
    project = load_config(config_file).project("example")
    assert project.atlassian.site_url == "https://example.atlassian.net"
    assert project.atlassian.max_linked_issues == 5
    assert project.atlassian.max_confluence_pages == 8
    assert project.context.instructions == ("AGENTS.md", "docs/architecture.md")
    assert project.investigation.max_prompt_bytes == 524288


@pytest.mark.parametrize(
    "extra",
    [
        'site_url = "http://example.atlassian.net"',
        'site_url = "https://user:password@example.atlassian.net"',
        'site_url = "https://example.atlassian.net/wiki"',
        'site_url = "https://example.atlassian.net:bad"',
        'site_url = "https://[invalid"',
        'auth_method = "password"',
        'token_env = "HOME"',
        'email_env = "bad-name"',
        'cloud_id = "../tenant"',
        'acceptance_fields = ["description"]',
        "confluence_page_ids = [42]",
        "max_linked_issues = 21",
        "max_confluence_pages = -1",
        'search_confluence = "yes"',
        "timeout_seconds = 0",
        "max_response_bytes = true",
        'api_token = "disallowed"',
    ],
)
def test_invalid_atlassian_configuration(config_file: Path, extra: str) -> None:
    """Reject unsupported origins, credential settings and retrieval limits."""
    values = {
        "site_url": '"https://example.atlassian.net"',
        "email_env": '"TASK_EMAIL"',
        "token_env": '"TASK_TOKEN"',
    }
    key, value = extra.split(" = ", 1)
    values[key] = value
    config_file.write_text(
        config_file.read_text()
        + "\n[projects.example.atlassian]\n"
        + "\n".join(f"{key} = {value}" for key, value in values.items())
    )
    with pytest.raises(DevAgentError):
        load_config(config_file)


@pytest.mark.parametrize(
    "table,setting",
    [
        ("context", 'instructions = ["../outside"]'),
        ("context", 'instructions = ["/etc/passwd"]'),
        ("context", 'instructions = ["C:/outside"]'),
        ("context", 'instructions = ["AGENTS.md", "AGENTS.md"]'),
        ("context", 'instructions = ["AGENTS.md"]\noptional_instructions = ["AGENTS.md"]'),
        ("context", "max_total_bytes = 0"),
        ("context", "max_document_bytes = true"),
        ("investigation", "max_snapshot_bytes = 0"),
        ("investigation", "max_snapshot_files = 50001"),
        ("investigation", "max_prompt_bytes = 2097153"),
        ("investigation", 'sandbox = "danger-full-access"'),
    ],
)
def test_invalid_context_and_snapshot_configuration(
    config_file: Path,
    table: str,
    setting: str,
) -> None:
    """Reject unsafe instructions and invalid snapshot safety bounds."""
    config_file.write_text(config_file.read_text() + f"\n[projects.example.{table}]\n{setting}\n")
    with pytest.raises(DevAgentError):
        load_config(config_file)
