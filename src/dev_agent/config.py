"""Strict, external TOML configuration. Only operator configuration supplies commands."""

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_agent.errors import DevAgentError

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def identifier(value: str, label: str = "identifier") -> str:
    if not NAME.fullmatch(value):
        raise DevAgentError(
            f"Invalid {label}: use 1-64 ASCII letters, digits, underscores or hyphens"
        )
    return value


def table(value: object, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DevAgentError(f"{label} must be a TOML table")
    if extra := value.keys() - allowed:
        raise DevAgentError(f"Unknown {label} settings: {', '.join(sorted(extra))}")
    return value


def string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise DevAgentError(f"{label} must be a nonempty string without NUL characters")
    return value


def seconds(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 604800:
        raise DevAgentError(f"{label} must be an integer between 1 and 604800 seconds")
    return value


def path(value: object, parent: Path, label: str) -> Path:
    candidate = Path(string(value, label)).expanduser()
    return (candidate if candidate.is_absolute() else parent / candidate).resolve()


def overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


@dataclass(frozen=True)
class AgentConfig:
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = 1800


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 600


@dataclass(frozen=True)
class Project:
    name: str
    repository: Path
    base_branch: str
    worktree_root: Path
    verification: tuple[Check, ...]
    agent: AgentConfig


@dataclass(frozen=True)
class Config:
    path: Path
    runs_root: Path
    projects: dict[str, Project]

    def project(self, name: str) -> Project:
        try:
            return self.projects[name]
        except KeyError:
            raise DevAgentError(f"Unknown project {name!r}; use 'projects list'") from None


def default_config_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dev-agent/config.toml"
    )


def load_config(filename: Path) -> Config:
    filename = filename.expanduser().resolve()
    try:
        data = tomllib.loads(filename.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DevAgentError(f"Cannot read configuration {filename}: {exc}") from exc
    table(data, {"runs_root", "projects"}, "configuration")
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
            raw, {"repository", "base_branch", "worktree_root", "verification", "agent"}, name
        )
        repo = path(raw.get("repository"), filename.parent, f"{name}.repository")
        root = path(raw.get("worktree_root"), filename.parent, f"{name}.worktree_root")
        branch = string(raw.get("base_branch"), f"{name}.base_branch")
        if branch.startswith("-") or any(c.isspace() for c in branch):
            raise DevAgentError(f"Invalid base branch for {name}")
        checks = []
        raw_checks = raw.get("verification", [])
        if not isinstance(raw_checks, list):
            raise DevAgentError(f"{name}.verification must be an array of tables")
        for check in raw_checks:
            check = table(check, {"name", "argv", "timeout_seconds"}, "verification")
            check_name = identifier(string(check.get("name"), "check name"), "check name")
            argv = check.get("argv")
            if not isinstance(argv, list) or not argv:
                raise DevAgentError("Verification argv must be a nonempty array of strings")
            args = tuple(string(arg, "verification argument") for arg in argv)
            if args[0].startswith("-"):
                raise DevAgentError("Verification executable cannot begin with '-'")
            if any(c.name == check_name for c in checks):
                raise DevAgentError(f"Duplicate verification name: {check_name}")
            checks.append(
                Check(check_name, args, seconds(check.get("timeout_seconds", 600), "timeout"))
            )
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
        )
    # Every storage root must be outside every configured source and its Git metadata.
    for project in projects.values():
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
    return Config(filename, runs, projects)
