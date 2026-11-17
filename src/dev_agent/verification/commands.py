"""Resolve and execute operator-defined preparation and verification commands.

Commands run with a scoped subprocess environment and worktree-relative locations. The workflow
supplies identity validation. Configured environment values remain out of command metadata.
"""

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from dev_agent.config import Check, Project, relative_path
from dev_agent.errors import DevAgentError
from dev_agent.process import ProcessInterrupted, environment, execute
from dev_agent.records import RunRecord, now


def contained_path(worktree: Path, relative: str) -> Path:
    """Resolve a configured path below the worktree and reject symlink escapes."""
    relative_path(relative, "worktree path")
    candidate = worktree / relative
    if not candidate.resolve().is_relative_to(worktree.resolve()):
        raise DevAgentError(f"Path escapes the worktree: {relative}")
    return candidate


def command_record(command: Check) -> RunRecord:
    """Initialise evidence for a trusted command without storing environment values."""
    # Values may be secrets. Only names and trusted argv are recorded, never env values.
    return {
        "name": command.name,
        "argv": list(command.argv),
        "cwd": command.cwd,
        "timeout_seconds": command.timeout_seconds,
        "required": command.required,
        "environment_names": sorted(command.env),
        "inherit_env": list(command.inherit_env),
        "required_env": list(command.required_env),
        "required_paths": list(command.required_paths),
        "status": "not_run",
        "exit_code": None,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "timed_out": False,
    }


def command_environment(
    project: Project, worktree: Path, command: Check | None = None
) -> dict[str, str]:
    """Build a command environment from allowlisted inheritance and configured values."""
    env = environment()
    inherited = project.inherit_env + (command.inherit_env if command else ())
    env.update({k: os.environ[k] for k in inherited if k in os.environ})
    values = project.env | (command.env if command else {})
    env.update({key: value.replace("{worktree}", str(worktree)) for key, value in values.items()})
    return env


def missing_requirements(
    project: Project,
    worktree: Path,
    command: Check | None = None,
    *,
    project_requirements: bool = True,
) -> list[str]:
    """List missing variables, executables and worktree paths before execution."""
    env = command_environment(project, worktree, command)
    required_env = project.inherit_env + (project.prerequisites.env if project_requirements else ())
    paths = project.prerequisites.paths if project_requirements else ()
    if command:
        required_env += command.required_env + command.inherit_env
        paths += command.required_paths
    missing = [f"Missing environment variable: {key}" for key in required_env if not env.get(key)]
    for relative in paths:
        try:
            if not contained_path(worktree, relative).exists():
                missing.append(f"Missing required worktree path: {relative}")
        except DevAgentError as exc:
            missing.append(str(exc))
    for executable in project.prerequisites.executables if project_requirements else ():
        if "/" in executable or "\\" in executable:
            raise DevAgentError("Prerequisite executables must be PATH command names")
        if not shutil.which(executable, path=env.get("PATH", "")):
            missing.append(f"Missing executable: {executable}")
    return sorted(set(missing))


def prerequisites(project: Project, worktree: Path, checks: tuple[Check, ...]) -> RunRecord:
    """Combine project and command requirements into a recorded preflight result."""
    failures = missing_requirements(project, worktree)
    for check in checks:
        failures += [f"{check.name}: {e}" for e in missing_requirements(project, worktree, check)]
        # Cwd is part of the declared command prerequisites as well.
        try:
            if not contained_path(worktree, check.cwd).is_dir():
                failures.append(f"{check.name}: working directory is missing: {check.cwd}")
        except DevAgentError as exc:
            failures.append(f"{check.name}: {exc}")
    return {
        "status": "failed" if failures else "passed",
        "failures": sorted(set(failures)),
        "checked_at": now(),
    }


def run_command(
    project: Project,
    worktree: Path,
    command: Check,
    record: RunRecord,
    directory: Path,
    save: Callable[[], None],
    *,
    project_requirements: bool = True,
) -> None:
    """Execute one configured command and update its checkpoint evidence.

    Revalidate the working directory and declared requirements, capture private logs and save
    duration and exit status. Missing prerequisites and process failures receive distinct
    failure kinds. A process interruption records the interrupted result and propagates.
    """
    started = time.monotonic()
    record.update(status="running", started_at=now())
    save()
    try:
        cwd = contained_path(worktree, command.cwd)
        missing = missing_requirements(
            project, worktree, command, project_requirements=project_requirements
        )
        if not cwd.is_dir():
            missing.append(f"Missing working directory: {command.cwd}")
        if missing:
            record.update(
                status="failed", failure_kind="missing_prerequisite", error="; ".join(missing)
            )
            return
        out = directory / f"check-{command.name}.stdout.log"
        err = directory / f"check-{command.name}.stderr.log"
        record.update(stdout_log=str(out), stderr_log=str(err))
        result = execute(
            list(command.argv),
            cwd=cwd,
            stdout=out,
            stderr=err,
            timeout=command.timeout_seconds,
            env=command_environment(project, worktree, command),
        )
        record.update(result.record(), status="passed" if result.ok else "failed")
        if not result.ok:
            record["failure_kind"] = (
                "timeout"
                if result.timed_out
                else "missing_prerequisite"
                if result.failure_kind == "missing_launch_prerequisite"
                else result.failure_kind or "command_failed"
            )
    except KeyboardInterrupt as exc:
        if isinstance(exc, ProcessInterrupted):
            record.update(exc.result.record())
        record.update(status="interrupted", failure_kind="interrupted")
        raise
    except DevAgentError as exc:
        record.update(status="failed", failure_kind="missing_prerequisite", error=str(exc))
    except Exception as exc:
        record.update(status="error", failure_kind="orchestration_error", error=str(exc))
        raise
    finally:
        record["finished_at"] = now()
        if record.get("duration_seconds") is None:
            record["duration_seconds"] = time.monotonic() - started
        save()


def prepare(
    project: Project,
    worktree: Path,
    state: RunRecord,
    directory: Path,
    save: Callable[[], None],
    validate: Callable[[], object],
) -> bool:
    """Run configured setup commands in order and checkpoint their progress.

    The worktree is revalidated before each command. Return False after a required failure.
    Optional failures are retained as warnings while later steps may continue.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.update(status="running", commands=[command_record(c) for c in project.preparation])
    save()
    for command, record in zip(project.preparation, state["commands"], strict=True):
        validate()
        run_command(project, worktree, command, record, directory, save, project_requirements=False)
        if record["status"] != "passed" and command.required:
            state["status"] = "failed"
            save()
            return False
    state["status"] = (
        "skipped"
        if not project.preparation
        else "optional_failed"
        if any(c["status"] != "passed" for c in state["commands"])
        else "passed"
    )
    save()
    return True
