"""Load the operator's TOML and construct typed project settings.

Only this configuration supplies trusted commands, paths and integration settings. Parsing
rejects unknown options and overlapping storage. Workflow preflight checks repository or
executable state separately.
"""

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from dev_agent.errors import DevAgentError

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def identifier(value: str, label: str = "identifier") -> str:
    """Validate the short ASCII identifiers used in task and artifact paths."""
    if not NAME.fullmatch(value):
        raise DevAgentError(
            f"Invalid {label}: use 1-64 ASCII letters, digits, underscores or hyphens"
        )
    return value


def table(value: object, allowed: set[str], label: str) -> dict[str, Any]:
    """Require a TOML table with keys from the caller's permitted set."""
    if not isinstance(value, dict):
        raise DevAgentError(f"{label} must be a TOML table")
    if extra := value.keys() - allowed:
        raise DevAgentError(f"Unknown {label} settings: {', '.join(sorted(extra))}")
    return value


def string(value: object, label: str) -> str:
    """Require a nonblank string without NUL characters and preserve its value."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise DevAgentError(f"{label} must be a nonempty string without NUL characters")
    return value


def seconds(value: object, label: str) -> int:
    """Accept integer timeouts from one second to seven days."""
    if type(value) is not int or not 1 <= value <= 604800:
        raise DevAgentError(f"{label} must be an integer between 1 and 604800 seconds")
    return value


def path(value: object, parent: Path, label: str) -> Path:
    """Expand a path and resolve relative values against the configuration directory."""
    candidate = Path(string(value, label)).expanduser()
    return (candidate if candidate.is_absolute() else parent / candidate).resolve()


def overlaps(a: Path, b: Path) -> bool:
    """Report whether either resolved path contains the other or equals it."""
    return a == b or a in b.parents or b in a.parents


def relative_path(value: object, label: str) -> str:
    """Reject absolute, drive-qualified and traversal paths before filesystem checks."""
    value = string(value, label)
    if (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or "\\" in value
        or ".." in PurePosixPath(value).parts
        or value.startswith("~")
    ):
        raise DevAgentError(f"{label} must be relative to the worktree without traversal")
    return value


def names(value: object, label: str) -> tuple[str, ...]:
    """Validate a list of environment variable names and freeze it as a tuple."""
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not ENV_NAME.fullmatch(v) for v in value
    ):
        raise DevAgentError(f"{label} must be an array of environment variable names")
    return tuple(value)


def env_values(value: object) -> dict[str, str]:
    """Validate an environment table whose values are NUL-free strings."""
    if not isinstance(value, dict):
        raise DevAgentError("env must be a table of explicit string values")
    names(list(value), "env")
    if any(not isinstance(v, str) or "\x00" in v for v in value.values()):
        raise DevAgentError("env values must be strings without NUL")
    return value


def default_results_root() -> Path:
    """Derive the results default from the installed module, including editable checkouts."""
    # Stable across invocation directories for source/editable installs.
    return Path(__file__).resolve().parents[2] / "results"


@dataclass(frozen=True)
class AgentConfig:
    """Hold per-project Codex model, reasoning effort and execution timeout settings."""

    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = 1800


@dataclass(frozen=True)
class Check:
    """Describe a trusted command, its worktree location, inputs and execution limits."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 600
    cwd: str = "."
    required: bool = True
    env: dict[str, str] = field(default_factory=dict)
    inherit_env: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    required_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prerequisites:
    """List project-wide variables, files and executables required for verification."""

    env: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    executables: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparationCache:
    """Name the dependency inputs and installed files needed to reuse preparation."""

    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class AtlassianConfig:
    """Describe bounded REST investigation and refer to credentials by environment name."""

    site_url: str
    email_env: str
    token_env: str
    confluence_token_env: str | None = None
    cloud_id: str | None = None
    auth_method: str = "api_token"
    acceptance_fields: tuple[str, ...] = ()
    discover_acceptance_fields: bool = True
    search_confluence: bool = True
    confluence_page_ids: tuple[str, ...] = ()
    max_linked_issues: int = 5
    max_confluence_pages: int = 8
    timeout_seconds: int = 30
    max_response_bytes: int = 2097152


@dataclass(frozen=True)
class RepositoryContext:
    """Control mandatory baseline instruction resolution, approved scopes and audit bounds."""

    # Legacy names remain aliases for explicit architectural context, never policy switches.
    instructions: tuple[str, ...] = ()
    optional_instructions: tuple[str, ...] = ()
    max_document_bytes: int = 65536
    max_total_bytes: int = 262144
    instruction_filename: str = "AGENTS.md"
    require_root_instructions: bool = True
    enforce_nested_instructions: bool = True
    required_documents: tuple[str, ...] = ()
    max_prompt_bytes: int = 1048576
    implementation_scope: tuple[str, ...] = (".",)
    max_audit_files: int = 200000
    max_audit_bytes: int = 2147483648


@dataclass(frozen=True)
class InvestigationConfig:
    """Limit sealed snapshot size, file count and read-only investigation prompt size."""

    max_snapshot_bytes: int = 134217728
    max_snapshot_files: int = 20000
    max_prompt_bytes: int = 524288


@dataclass(frozen=True)
class GitHubConfig:
    """Identify the publication destination, permitted paths and CI polling bounds."""

    repository: str
    allowed_paths: tuple[str, ...]
    ci_timeout_seconds: int = 600
    poll_interval_seconds: int = 10


@dataclass(frozen=True)
class Project:
    """Collect the settings for one managed repository and its execution policies."""

    name: str
    repository: Path
    base_branch: str
    worktree_root: Path
    verification: tuple[Check, ...]
    agent: AgentConfig
    preparation: tuple[Check, ...] = ()
    verification_profiles: dict[str, tuple[Check, ...]] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    inherit_env: tuple[str, ...] = ()
    prerequisites: Prerequisites = field(default_factory=Prerequisites)
    atlassian: AtlassianConfig | None = None
    context: RepositoryContext = field(default_factory=RepositoryContext)
    investigation: InvestigationConfig = field(default_factory=InvestigationConfig)
    github: GitHubConfig | None = None
    preparation_cache: PreparationCache | None = None

    def checks(self, profile: str | None) -> tuple[str, tuple[Check, ...]]:
        """Resolve a check profile, preferring targeted by default when it is configured.

        An explicit legacy request uses the original verification list. A named profile that is
        absent raises DevAgentError.
        """
        if profile is None:
            profile = "targeted" if "targeted" in self.verification_profiles else "legacy"
        if profile == "legacy":
            return profile, self.verification
        if profile not in self.verification_profiles:
            raise DevAgentError(
                f"Verification profile {profile!r} is not configured for {self.name}"
            )
        return profile, self.verification_profiles[profile]


@dataclass(frozen=True)
class Config:
    """Hold the loaded project registry, canonical roots and configuration file digest."""

    path: Path
    runs_root: Path
    projects: dict[str, Project]
    results_root: Path = field(default_factory=default_results_root)
    source_digest: str | None = None

    def project(self, name: str) -> Project:
        """Look up a configured project by name or raise an actionable error."""
        try:
            return self.projects[name]
        except KeyError:
            raise DevAgentError(f"Unknown project {name!r}; use 'projects list'") from None


def default_config_path() -> Path:
    """Locate the operator TOML under XDG_CONFIG_HOME or the user's config directory."""
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dev-agent/config.toml"
    )


def load_config(filename: Path) -> Config:
    """Read and validate all projects from one TOML file.

    Relative settings are resolved against the chosen file. The returned source digest describes
    the exact bytes parsed and is recorded for resumption and verification. The function checks
    storage separation without creating directories.

    Raises:
        DevAgentError: If the TOML cannot be read or a configuration requirement fails.
    """
    filename = filename.expanduser().resolve()
    try:
        content = filename.read_bytes()
        data = tomllib.loads(content.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise DevAgentError(f"Cannot read configuration {filename}: {exc}") from exc
    table(data, {"runs_root", "results_root", "projects"}, "configuration")
    results = path(
        data.get("results_root", str(default_results_root())), filename.parent, "results_root"
    )
    default_runs = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    runs = path(
        data.get("runs_root", str(default_runs / "dev-agent/runs")), filename.parent, "runs_root"
    )
    raw_projects = data.get("projects")
    if not isinstance(raw_projects, dict) or not raw_projects:
        raise DevAgentError("Configure at least one project in [projects.NAME]")
    projects = {}
    for name, raw in raw_projects.items():
        identifier(name, "project name")
        raw = table(
            raw,
            {
                "repository",
                "base_branch",
                "worktree_root",
                "verification",
                "agent",
                "preparation",
                "preparation_cache",
                "verification_profiles",
                "env",
                "inherit_env",
                "prerequisites",
                "atlassian",
                "context",
                "investigation",
                "github",
            },
            name,
        )
        repo = path(raw.get("repository"), filename.parent, f"{name}.repository")
        root = path(raw.get("worktree_root"), filename.parent, f"{name}.worktree_root")
        branch = string(raw.get("base_branch"), f"{name}.base_branch")
        if branch.startswith("-") or any(c.isspace() for c in branch):
            raise DevAgentError(f"Invalid base branch for {name}")
        checks = parse_commands(raw.get("verification", []), "verification")
        preparation = parse_commands(raw.get("preparation", []), "preparation")
        profiles = table(
            raw.get("verification_profiles", {}), {"targeted", "full"}, "verification_profiles"
        )
        profiles = {key: parse_commands(value, "verification") for key, value in profiles.items()}
        prereq = table(
            raw.get("prerequisites", {}), {"env", "paths", "executables"}, "prerequisites"
        )
        paths = prereq.get("paths", [])
        executables = prereq.get("executables", [])
        if not isinstance(paths, list) or not isinstance(executables, list):
            raise DevAgentError("Prerequisite paths and executables must be arrays")
        agent = table(
            raw.get("agent", {}), {"model", "reasoning_effort", "timeout_seconds"}, "agent"
        )
        model = string(agent["model"], "model") if "model" in agent else None
        effort = agent.get("reasoning_effort")
        if effort is not None and effort not in (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        ):
            raise DevAgentError("Unsupported reasoning_effort")
        projects[name] = Project(
            name,
            repo,
            branch,
            root,
            tuple(checks),
            AgentConfig(
                model, effort, seconds(agent.get("timeout_seconds", 1800), "agent timeout")
            ),
            preparation,
            profiles,
            env_values(raw.get("env", {})),
            names(raw.get("inherit_env", []), "inherit_env"),
            Prerequisites(
                names(prereq.get("env", []), "prerequisites.env"),
                tuple(relative_path(p, "prerequisite path") for p in paths),
                tuple(executable_name(e) for e in executables),
            ),
            parse_atlassian(raw["atlassian"]) if "atlassian" in raw else None,
            parse_context(raw.get("context", {})),
            parse_investigation(raw.get("investigation", {})),
            parse_github(raw["github"]) if "github" in raw else None,
            parse_preparation_cache(raw["preparation_cache"])
            if "preparation_cache" in raw
            else None,
        )
    # Every storage root must be outside every configured source and its Git metadata.
    for project in projects.values():
        if any(overlaps(results, p) for p in (project.repository, project.worktree_root, runs)):
            raise DevAgentError(
                "results_root must be outside managed repositories, "
                "worktrees and canonical run storage"
            )
        for root in (runs, *(p.worktree_root for p in projects.values())):
            if overlaps(root, project.repository):
                raise DevAgentError("Run/worktree storage must be outside all project repositories")
        if overlaps(runs, project.worktree_root):
            raise DevAgentError("runs_root and worktree_root must not overlap")
        for root in (runs, project.worktree_root):
            if root.exists() and not root.is_dir():
                raise DevAgentError(f"Storage root is not a directory: {root}")
            # Do not put records or task worktrees inside some other repository either.
            if any(
                (p / ".git").is_file()
                or (p / ".git" / "HEAD").is_file()
                or ((p / "HEAD").is_file() and (p / "objects").is_dir())
                for p in (root, *root.parents)
            ):
                raise DevAgentError(f"Storage root is inside a Git repository: {root}")
    if results.exists() and not results.is_dir():
        raise DevAgentError("results_root must be a directory")
    if any(
        (p / "HEAD").is_file() and (p / "objects").is_dir() for p in (results, *results.parents)
    ):
        raise DevAgentError("results_root must not be inside Git metadata")
    return Config(filename, runs, projects, results, hashlib.sha256(content).hexdigest())


def parse_github(value: object) -> GitHubConfig:
    """Validate an owner/repository destination, literal file allowlist and CI timings."""
    raw = table(
        value,
        {"repository", "allowed_paths", "ci_timeout_seconds", "poll_interval_seconds"},
        "github",
    )
    repository = string(raw.get("repository"), "github.repository")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise DevAgentError("github.repository must be OWNER/REPO on github.com")
    paths = raw.get("allowed_paths", [])
    if not isinstance(paths, list) or not paths:
        raise DevAgentError("github.allowed_paths must explicitly list source files/directories")
    checked = tuple(relative_path(p, "github.allowed_paths") for p in paths)
    if any(p in ("", ".") or any(c in p for c in "*?[") for p in checked):
        raise DevAgentError(
            "github.allowed_paths must use literal files/directories, not . or globs"
        )
    return GitHubConfig(
        repository,
        checked,
        seconds(raw.get("ci_timeout_seconds", 600), "github.ci_timeout_seconds"),
        seconds(raw.get("poll_interval_seconds", 10), "github.poll_interval_seconds"),
    )


def parse_preparation_cache(value: object) -> PreparationCache:
    """Require bounded, literal worktree file paths for both sides of cache validation."""
    raw = table(value, {"inputs", "outputs"}, "preparation_cache")

    def files(key: str) -> tuple[str, ...]:
        """Validate one list without assuming a particular package manager."""
        values = raw.get(key)
        label = f"preparation_cache.{key}"
        if not isinstance(values, list) or not 1 <= len(values) <= 64:
            raise DevAgentError(f"{label} must contain 1-64 literal file paths")
        paths = tuple(relative_path(path, label) for path in values)
        if len(set(paths)) != len(paths) or any(
            path == "." or any(c in path for c in "*?[") for path in paths
        ):
            raise DevAgentError(f"{label} must contain unique literal file paths")
        return paths

    return PreparationCache(files("inputs"), files("outputs"))


def parse_commands(value: object, label: str) -> tuple[Check, ...]:
    """Convert trusted command tables into ordered checks with unique names.

    Argument vectors are nonempty. Only preparation commands accept an optional required
    setting. Paths are syntactically relative here and checked against the real worktree at
    execution.
    """
    if not isinstance(value, list):
        raise DevAgentError(f"{label} must be an array of tables")
    result = []
    for raw in value:
        allowed = {
            "name",
            "argv",
            "timeout_seconds",
            "cwd",
            "env",
            "inherit_env",
            "required_env",
            "required_paths",
        }
        if label == "preparation":
            allowed.add("required")
        raw = table(raw, allowed, label)
        name = identifier(string(raw.get("name"), "check name"), "check name")
        if any(c.name == name for c in result):
            raise DevAgentError(f"Duplicate {label} name: {name}")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv:
            raise DevAgentError("Command argv must be a nonempty array of strings")
        args = tuple(string(a, "command argument") for a in argv)
        if args[0].startswith("-"):
            raise DevAgentError("Command executable cannot begin with '-'")
        required = raw.get("required", True)
        if type(required) is not bool:
            raise DevAgentError("required must be a boolean")
        paths = raw.get("required_paths", [])
        if not isinstance(paths, list):
            raise DevAgentError("required_paths must be an array")
        result.append(
            Check(
                name,
                args,
                seconds(raw.get("timeout_seconds", 600), "timeout"),
                relative_path(raw.get("cwd", "."), "cwd"),
                required,
                env_values(raw.get("env", {})),
                names(raw.get("inherit_env", []), "inherit_env"),
                names(raw.get("required_env", []), "required_env"),
                tuple(relative_path(p, "required path") for p in paths),
            )
        )
    return tuple(result)


def executable_name(value: object) -> str:
    """Accept a prerequisite executable name for lookup on PATH."""
    value = string(value, "prerequisite executable")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
        raise DevAgentError("Prerequisite executables must be PATH command names")
    return value


def bounded(value: object, low: int, high: int, label: str) -> int:
    """Require a real integer inside the caller's inclusive bounds."""
    if type(value) is not int or not low <= value <= high:
        raise DevAgentError(f"{label} must be an integer between {low} and {high}")
    return value


def boolean(value: object, label: str) -> bool:
    """Require a TOML boolean instead of an integer or string."""
    if type(value) is not bool:
        raise DevAgentError(f"{label} must be a boolean")
    return value


def parse_atlassian(value: object) -> AtlassianConfig:
    """Validate the HTTPS REST origin, credential names and retrieval limits.

    Only API-token authentication is implemented by this provider. Credential names must not
    overlap the environment variables retained for agent subprocesses.
    """
    raw = table(value, set(AtlassianConfig.__dataclass_fields__), "atlassian")
    site = string(raw.get("site_url"), "atlassian.site_url").rstrip("/")
    try:
        url = urlsplit(site)
        port = url.port
    except ValueError:
        raise DevAgentError("Invalid Atlassian site_url HTTPS origin") from None
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
        or port not in (None, 443)
    ):
        raise DevAgentError(
            "Atlassian site_url must be an HTTPS origin without credentials, path or query"
        )
    if raw.get("auth_method", "api_token") != "api_token":
        raise DevAgentError("Only Atlassian auth_method = 'api_token' is implemented")
    credential_names = []
    for key in ("email_env", "token_env", "confluence_token_env"):
        if key == "confluence_token_env" and key not in raw:
            continue
        name = string(raw.get(key), f"atlassian.{key}")
        names([name], key)
        if name in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "CODEX_HOME"}:
            raise DevAgentError(
                "Credential env names must not overlap the inherited agent environment"
            )
        credential_names.append(name)
    cloud_id = raw.get("cloud_id")
    if cloud_id is not None and (
        not isinstance(cloud_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", cloud_id)
    ):
        raise DevAgentError("Invalid Atlassian cloud_id")
    acceptance = raw.get("acceptance_fields", [])
    pages = raw.get("confluence_page_ids", [])
    if (
        not isinstance(acceptance, list)
        or len(acceptance) > 32
        or any(
            not isinstance(v, str) or not re.fullmatch(r"customfield_[0-9]+", v) for v in acceptance
        )
    ):
        raise DevAgentError("acceptance_fields must contain at most 32 customfield_NUMBER IDs")
    if (
        not isinstance(pages, list)
        or len(pages) > 20
        or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,32}", v) for v in pages)
    ):
        raise DevAgentError("confluence_page_ids must contain at most 20 numeric string IDs")
    return AtlassianConfig(
        site,
        *credential_names[:2],
        confluence_token_env=credential_names[2] if len(credential_names) == 3 else None,
        cloud_id=cloud_id,
        acceptance_fields=tuple(dict.fromkeys(acceptance)),
        discover_acceptance_fields=boolean(
            raw.get("discover_acceptance_fields", True), "discover_acceptance_fields"
        ),
        search_confluence=boolean(raw.get("search_confluence", True), "search_confluence"),
        confluence_page_ids=tuple(dict.fromkeys(pages)),
        max_linked_issues=bounded(raw.get("max_linked_issues", 5), 0, 20, "max_linked_issues"),
        max_confluence_pages=bounded(
            raw.get("max_confluence_pages", 8), 0, 20, "max_confluence_pages"
        ),
        timeout_seconds=bounded(raw.get("timeout_seconds", 30), 1, 120, "HTTP timeout"),
        max_response_bytes=bounded(
            raw.get("max_response_bytes", 2097152), 1024, 8388608, "max_response_bytes"
        ),
    )


def parse_context(value: object) -> RepositoryContext:
    """Validate instruction inputs, literal scopes and resource limits.

    The root AGENTS.md and nested hierarchy remain mandatory. Additional architecture documents
    are configured separately from instruction discovery.
    """
    raw = table(value, set(RepositoryContext.__dataclass_fields__), "context")
    paths = {}
    for key in ("instructions", "optional_instructions", "required_documents"):
        values = raw.get(key, [])
        if not isinstance(values, list) or len(values) > 32:
            raise DevAgentError(f"context.{key} must be an array of at most 32 paths")
        paths[key] = tuple(relative_path(v, key) for v in values)
    all_paths = sum(paths.values(), ())
    if len(set(all_paths)) != len(all_paths):
        raise DevAgentError("Repository context paths must be unique")
    if raw.get("instruction_filename", "AGENTS.md") != "AGENTS.md":
        raise DevAgentError(
            "instruction_filename must be AGENTS.md; alternate names are unsupported"
        )
    for key in ("require_root_instructions", "enforce_nested_instructions"):
        if raw.get(key, True) is not True:
            raise DevAgentError(f"context.{key} is mandatory and must be true")
    if any(PurePosixPath(p).name == "AGENTS.md" for p in paths["optional_instructions"]):
        raise DevAgentError("AGENTS.md cannot be optional; remove it from optional_instructions")
    scopes = raw.get("implementation_scope", ["."])
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 64:
        raise DevAgentError("implementation_scope must contain 1-64 literal relative paths")
    scopes = tuple(relative_path(p, "implementation scope") for p in scopes)
    if len(set(scopes)) != len(scopes):
        raise DevAgentError("Implementation scopes must be unique")
    return RepositoryContext(
        **paths,
        max_document_bytes=bounded(
            raw.get("max_document_bytes", 65536), 1, 1048576, "max_document_bytes"
        ),
        max_total_bytes=bounded(raw.get("max_total_bytes", 262144), 1, 2097152, "max_total_bytes"),
        max_prompt_bytes=bounded(
            raw.get("max_prompt_bytes", 1048576), 1024, 4194304, "context.max_prompt_bytes"
        ),
        implementation_scope=scopes,
        max_audit_files=bounded(raw.get("max_audit_files", 200000), 1, 1000000, "max_audit_files"),
        max_audit_bytes=bounded(
            raw.get("max_audit_bytes", 2147483648), 1, 17179869184, "max_audit_bytes"
        ),
    )


def parse_investigation(value: object) -> InvestigationConfig:
    """Validate byte and file limits for baseline snapshots and investigation prompts."""
    raw = table(value, set(InvestigationConfig.__dataclass_fields__), "investigation")
    return InvestigationConfig(
        max_snapshot_bytes=bounded(
            raw.get("max_snapshot_bytes", 134217728), 1024, 536870912, "max_snapshot_bytes"
        ),
        max_snapshot_files=bounded(
            raw.get("max_snapshot_files", 20000), 1, 50000, "max_snapshot_files"
        ),
        max_prompt_bytes=bounded(
            raw.get("max_prompt_bytes", 524288), 1024, 2097152, "max_prompt_bytes"
        ),
    )
