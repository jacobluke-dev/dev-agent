"""An explicit allowlist of local Git operations. Never invokes a shell or remote."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dev_agent.config import Project, overlaps
from dev_agent.errors import DevAgentError
from dev_agent.process import environment


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    env = environment() | {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "submodule.recurse=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "protocol.file.allow=never",
                "-C",
                str(repo),
                *args,
            ],
            env=env,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DevAgentError(f"Cannot execute Git: {exc}") from exc
    if check and result.returncode:
        raise DevAgentError(
            f"Git {args[0]} failed: {result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result


def output(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.decode("utf-8", errors="replace").strip()


@dataclass(frozen=True)
class Baseline:
    commit: str
    common_dir: Path
    source_dirty: bool


def validate_repository(project: Project, runs_root: Path) -> Baseline:
    repo = project.repository
    if not repo.is_dir():
        raise DevAgentError(f"Repository does not exist: {repo}")
    if output(repo, "rev-parse", "--is-bare-repository") != "false":
        raise DevAgentError("Bare repositories are not supported")
    if Path(output(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise DevAgentError("repository must be the Git working tree root")
    git(repo, "check-ref-format", f"refs/heads/{project.base_branch}")
    commit = output(repo, "rev-parse", "--verify", f"refs/heads/{project.base_branch}^{{commit}}")
    common = Path(output(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    for root in (project.worktree_root, runs_root):
        if overlaps(root, common):
            raise DevAgentError("Storage must not overlap shared Git metadata")
    # Checkout filters execute outside the agent sandbox. Fail closed instead of running them.
    filters = git(repo, "config", "--includes", "--get-regexp", r"^filter\.", check=False)
    if filters.returncode != 1:
        raise DevAgentError(
            "Repositories with local checkout filters are unsupported; use a clean clone"
        )
    return Baseline(commit, common, bool(status(repo)))


def status(repo: Path, *, ignored: bool = False) -> bytes:
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if ignored:
        args.append("--ignored")
    return git(repo, *args).stdout


def worktrees(repo: Path) -> dict[Path, dict[str, str]]:
    result = {}
    # NUL format also handles spaces and newlines in repository paths.
    current: dict[str, str] = {}
    for field in git(repo, "worktree", "list", "--porcelain", "-z").stdout.split(b"\x00"):
        if not field:
            if "worktree" in current:
                result[Path(current["worktree"]).resolve()] = current
            current = {}
        else:
            key, _, value = os.fsdecode(field).partition(" ")
            current[key] = value
    return result


def check_destination(project: Project, branch: str, destination: Path) -> None:
    git(project.repository, "check-ref-format", f"refs/heads/{branch}")
    exists = git(
        project.repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
    )
    if exists.returncode != 1:
        raise DevAgentError(f"Branch already exists or cannot be checked: {branch}")
    if (
        destination.exists()
        or destination.is_symlink()
        or destination in worktrees(project.repository)
    ):
        raise DevAgentError(f"Worktree destination already exists or is registered: {destination}")


def create_worktree(project: Project, branch: str, destination: Path, commit: str) -> None:
    check_destination(project, branch, destination)
    project.worktree_root.mkdir(parents=True, exist_ok=True)
    git(project.repository, "worktree", "add", "-b", branch, str(destination), commit)


def changed_files(worktree: Path, baseline: str) -> list[str]:
    tracked = git(
        worktree, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", baseline, "--"
    ).stdout
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard", "-z").stdout
    return sorted({os.fsdecode(p) for p in (tracked + untracked).split(b"\x00") if p})


def repository_instructions(repo: Path, commit: str) -> str:
    # Read the committed root instruction file as data, never as orchestration policy.
    spec = f"{commit}:AGENTS.md"
    info = git(repo, "ls-tree", commit, "--", "AGENTS.md").stdout
    if not info:
        return "(No committed root AGENTS.md.)"
    if not info.startswith((b"100644 blob ", b"100755 blob ")):
        raise DevAgentError("Root AGENTS.md must be a regular committed file")
    size = int(output(repo, "cat-file", "-s", spec))
    if size > 65536:
        raise DevAgentError("Root AGENTS.md exceeds the 64 KiB instruction limit")
    return git(repo, "show", spec).stdout.decode("utf-8", errors="replace")
