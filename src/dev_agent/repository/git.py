"""Run local Git operations for source and worktree management.

The controller supplies explicit Git arguments and disables hooks and selected configuration
layers. This module records exact source identity and fingerprints. Remote publication is
orchestrated separately.
"""

import hashlib
import os
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dev_agent.config import Config, Project, overlaps
from dev_agent.errors import DevAgentError
from dev_agent.process import environment
from dev_agent.records import RunRecord, now


def git(
    repo: Path, *args: str, check: bool = True, input: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run Git in the given repository and capture both streams as bytes.

    The caller selects local operations. Git receives a restricted environment with hooks and
    global and system configuration disabled. A nonzero result raises DevAgentError unless check
    is false.
    """
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
            input=input,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DevAgentError(f"Cannot execute Git: {exc}") from exc
    if check and result.returncode:
        raise DevAgentError(
            f"Git {args[0]} failed: {result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result


def output(repo: Path, *args: str) -> str:
    """Decode and trim stdout from a successful Git command."""
    return git(repo, *args).stdout.decode("utf-8", errors="replace").strip()


@dataclass(frozen=True)
class Baseline:
    """Bind an exact commit to its shared Git directory and source dirtiness."""

    commit: str
    common_dir: Path
    source_dirty: bool


def validate_repository(project: Project, runs_root: Path) -> Baseline:
    """Resolve the configured baseline and reject unsafe source or storage state.

    The source must be the top level of a non-bare repository. Validation disallows checkout
    filters and overlapping shared metadata. It does not fetch remote refs.
    """
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
    """Read NUL-delimited porcelain status, optionally including ignored files."""
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if ignored:
        args.append("--ignored")
    return git(repo, *args).stdout


def worktrees(repo: Path) -> dict[Path, dict[str, str]]:
    """Index Git's registered worktree entries by resolved working directory."""
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
    """Refuse an invalid or previously used task branch or worktree destination."""
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
    """Create the task branch and worktree at the given exact commit after collision checks."""
    check_destination(project, branch, destination)
    project.worktree_root.mkdir(parents=True, exist_ok=True)
    git(project.repository, "worktree", "add", "-b", branch, str(destination), commit)


def changed_files(worktree: Path, baseline: str) -> list[str]:
    """List tracked differences from the baseline and untracked nonignored paths."""
    tracked = git(
        worktree,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
        baseline,
        "--",
    ).stdout
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard", "-z").stdout
    return sorted({os.fsdecode(p) for p in (tracked + untracked).split(b"\x00") if p})


def source_paths(worktree: Path) -> set[bytes]:
    """Return index paths and Git's non-ignored untracked paths as literal bytes."""
    paths = git(worktree, "ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout
    return set(paths.split(b"\0")) - {b""}


def ignored_paths(worktree: Path, paths: Iterable[str]) -> set[str]:
    """Test ignore rules even for indexed paths, without trusting force-added entries."""
    payload = b"".join(os.fsencode(path) + b"\0" for path in paths)
    if not payload:
        return set()
    result = git(
        worktree, "check-ignore", "--no-index", "-z", "--stdin", input=payload, check=False
    )
    if result.returncode not in (0, 1):
        raise DevAgentError("Could not classify potentially ignored publication paths")
    return {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}


def fingerprint_entry(worktree: Path, name: bytes) -> bytes:
    """Encode an entry in the existing fingerprint format without following parent links."""
    parts = os.fsdecode(name).split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise DevAgentError("Unsafe source fingerprint path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(worktree, directory_flags)
    try:
        for part in parts[:-1]:
            mode = os.stat(part, dir_fd=directory, follow_symlinks=False).st_mode
            if stat.S_ISREG(mode):
                return b"missing"  # A regular file replaced an ancestor directory.
            if stat.S_ISLNK(mode):
                raise DevAgentError("Cannot fingerprint a source path through a symlink")
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        leaf = parts[-1]
        initial = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(initial.st_mode):
            target = os.fsencode(os.readlink(leaf, dir_fd=directory))
            return b"symlink\x00" + hashlib.sha256(target).digest()
        if not stat.S_ISREG(initial.st_mode):
            raise DevAgentError(
                "Source snapshots do not support submodules or special source files"
            )
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            current = os.fstat(stream.fileno())
            if not stat.S_ISREG(current.st_mode) or not os.path.samestat(initial, current):
                raise DevAgentError("Source changed while fingerprinting")
            return (
                f"{current.st_mode & 0o777}:".encode()
                + hashlib.file_digest(stream, "sha256").digest()
            )
    except FileNotFoundError:
        return b"missing"
    finally:
        os.close(directory)


def source_state(worktree: Path) -> RunRecord:
    """Fingerprint dirty source without writing the index or following source symlinks."""
    head = output(worktree, "rev-parse", "HEAD")
    index = git(worktree, "ls-files", "--stage", "-z").stdout
    digest = hashlib.sha256(head.encode() + hashlib.sha256(index).digest())
    for name in sorted(source_paths(worktree)):
        digest.update(name + b"\x00")
        digest.update(fingerprint_entry(worktree, name))
        digest.update(b"\x00")
    return {
        "head_commit": head,
        "fingerprint": digest.hexdigest(),
        "index_sha256": hashlib.sha256(index).hexdigest(),
        "dirty": bool(status(worktree)),
        "dirty_including_ignored": bool(
            git(worktree, "status", "--porcelain=v1", "-z", "--ignored=matching", "-unormal").stdout
        ),
        "scope": "HEAD, index, tracked and nonignored untracked contents/modes; "
        "ignored files excluded",
        "captured_at": now(),
    }


def validate_unstaged_worktree(root: Path) -> None:
    """Reject Git operations, staged changes and hidden/unmerged index entries."""
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
        "sequencer",
        "index.lock",
    ):
        if Path(output(root, "rev-parse", "--path-format=absolute", "--git-path", name)).exists():
            raise DevAgentError("Unexpected in-progress Git operation or index lock")
    if (
        git(
            root, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--quiet", check=False
        ).returncode
        != 0
    ):
        raise DevAgentError("Existing staged changes are unexpected; inspect and unstage manually")
    if any(p[:1] != b"H" for p in git(root, "ls-files", "-v", "-z").stdout.split(b"\0") if p):
        raise DevAgentError(
            "Skip-worktree/assume-unchanged or unresolved index entries are unsupported"
        )


def validate_managed_worktree(config: Config, record: RunRecord) -> Project:
    """Reconfirm the retained worktree's branch, Git metadata and configured identity.

    Return the project settings for an intact implementation worktree. Missing, replaced or
    moved worktrees and incompatible records raise DevAgentError.
    """
    project = config.project(record["project"])
    owner = record["adopted_from"] if record.get("kind") == "adoption" else record["run_id"]
    expected = project.worktree_root / owner
    if (
        record["repository"] != str(project.repository)
        or record["worktree_root"] != str(project.worktree_root)
        or record["runs_root"] != str(config.runs_root)
        or record["worktree"] != str(expected)
        or record["branch"] != f"dev-agent/{owner}"
        or expected.is_symlink()
        or expected.resolve() != expected
    ):
        raise DevAgentError(
            "Run paths/branch no longer match configuration; refusing managed operation"
        )
    common = Path(
        output(project.repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    if str(common) != record["git_common_dir"]:
        raise DevAgentError("Source repository identity changed")
    entry = worktrees(project.repository).get(expected)
    if not entry or entry.get("branch") != f"refs/heads/{record['branch']}":
        raise DevAgentError("Worktree registration/branch changed or missing; inspect manually")
    if not expected.is_dir():
        raise DevAgentError("Worktree is missing; inspect manually")
    if Path(output(expected, "rev-parse", "--show-toplevel")).resolve() != expected:
        raise DevAgentError("Unexpected working tree root")
    if (
        Path(output(expected, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        != common
    ):
        raise DevAgentError("Worktree Git metadata identity changed")
    if output(expected, "symbolic-ref", "HEAD") != f"refs/heads/{record['branch']}":
        raise DevAgentError("Worktree HEAD changed; inspect manually")
    return project
