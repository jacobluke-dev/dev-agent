"""Resolve and execute operator-defined preparation and verification commands.

Commands run with a scoped subprocess environment and worktree-relative locations. The workflow
supplies identity validation. Configured environment values remain out of command metadata.
"""

import hashlib
import json
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from dev_agent.config import Check, Project, relative_path
from dev_agent.errors import DevAgentError
from dev_agent.process import ProcessInterrupted, environment, execute
from dev_agent.process_identity import process_scope, process_state, recover_session
from dev_agent.records import RunRecord, now

MAX_PREPARATION_INPUT_BYTES = 32 * 1024 * 1024


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
    record.update(status="running", started_at=now(), controller_pid=os.getpid())
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
        if not project_requirements:
            try:
                controller = process_state(os.getpid())
                if controller:
                    record["process"] = {
                        "scope": process_scope(),
                        "controller": controller.identity(),
                    }
            except OSError, ValueError, IndexError:
                pass  # Keep running normally, with conservative recovery if interrupted.
        save()

        def child_started(pid: int) -> None:
            """Checkpoint the installer session before waiting on the subprocess."""
            evidence = record.get("process")
            if evidence is not None:
                try:
                    child = process_state(pid)
                except OSError, ValueError, IndexError:
                    return
                if child and child.session == child.pid == child.group:
                    evidence["child"] = {**child.identity(), "session": child.session}
                    save()

        result = execute(
            list(command.argv),
            cwd=cwd,
            stdout=out,
            stderr=err,
            timeout=command.timeout_seconds,
            env=command_environment(project, worktree, command),
            on_started=child_started if not project_requirements else None,
        )
        record.update(result.record(), status="passed" if result.ok else "failed")
        if not result.ok:
            record["failure_kind"] = (
                "cleanup_incomplete"
                if result.failure_kind == "cleanup_incomplete"
                else "timeout"
                if result.timed_out
                else "missing_prerequisite"
                if result.failure_kind == "missing_launch_prerequisite"
                else result.failure_kind or "command_failed"
            )
    except KeyboardInterrupt as exc:
        if isinstance(exc, ProcessInterrupted):
            record.update(exc.result.record())
        record["status"] = "interrupted"
        if record.get("failure_kind") != "cleanup_incomplete":
            record["failure_kind"] = "interrupted"
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


def preparation_fingerprint(project: Project, worktree: Path) -> str | None:
    """Identify configured setup, declared inputs, worktree and resolved executables."""
    if not project.preparation_cache:
        return None
    try:
        inputs = {}
        remaining = MAX_PREPARATION_INPUT_BYTES
        for name in project.preparation_cache.inputs:
            path = contained_path(worktree, name)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None
                content = stream.read(remaining + 1)
            remaining -= len(content)
            if remaining < 0:
                return None
            inputs[name] = hashlib.sha256(content).hexdigest()
        tools = []
        for command in project.preparation:
            cwd = contained_path(worktree, command.cwd)
            env = command_environment(project, worktree, command)
            executable = Path(command.argv[0])
            if "/" in command.argv[0]:
                path = cwd / executable
            else:
                search = os.pathsep.join(
                    str(cwd / part) for part in env.get("PATH", "").split(os.pathsep)
                )
                found = shutil.which(command.argv[0], path=search)
                if not found:
                    return None
                path = Path(found)
            tools.append([file_identity(path), env])
        directory = worktree.stat()
        return private_digest(
            {
                "version": 1,
                "worktree": [str(worktree), directory.st_dev, directory.st_ino],
                "inputs": inputs,
                "outputs": project.preparation_cache.outputs,
                "commands": [asdict(command) for command in project.preparation],
                "tools": tools,
            }
        )
    except OSError, DevAgentError, ValueError:
        return None  # Missing or unsafe cache evidence never qualifies for reuse.


def file_identity(path: Path) -> list[str | int]:
    """Return a resolved regular file identity without reading tool or output contents."""
    target = path.resolve(strict=True)
    info = target.stat()
    if not stat.S_ISREG(info.st_mode):
        raise DevAgentError("Preparation cache evidence must be a file")
    return [
        str(target),
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


def private_digest(value: object) -> str:
    """Summarise cache evidence without retaining environment values in the record."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def preparation_outputs(project: Project, worktree: Path) -> str | None:
    """Require declared installed files to exist and retain their recorded identities."""
    if not project.preparation_cache:
        return None
    try:
        return private_digest(
            [
                file_identity(contained_path(worktree, name))
                for name in project.preparation_cache.outputs
            ]
        )
    except OSError, DevAgentError, ValueError:
        return None


def preparation_recovery(prior: RunRecord, *, force: bool) -> RunRecord | None:
    """Reconcile previous setup under the caller's worktree lock without changing history."""
    status = prior.get("status")
    if status not in ("running", "uncertain", "interrupted", "failed", "error"):
        return None
    commands = prior.get("commands", [])
    legacy = not commands and status in ("running", "uncertain")
    checked = []
    for command in commands:
        command_status = command.get("status")
        if command_status == "not_run":
            continue
        if command_status not in ("running", "passed", "failed", "interrupted", "error"):
            return {"decision": "blocked", "reason": "Previous setup has invalid command evidence"}
        unresolved = (
            command.get("status") == "running"
            or command.get("failure_kind") == "cleanup_incomplete"
            or (
                command.get("status") in ("error", "interrupted")
                and command.get("exit_code") is None
            )
        )
        if evidence := command.get("process"):
            if evidence.get("child") or unresolved:
                result = recover_session(evidence)
                if result["decision"] == "blocked":
                    return result
                checked.append(result["reason"])
        elif unresolved:
            legacy = True
    if legacy:
        reason = (
            "Previous setup has no durable installer identity. Inspect surviving processes "
            "before proceeding. For a legacy adoption, --prepare records an operator retry."
        )
        return {"decision": "operator_retry" if force else "blocked", "reason": reason}
    if checked or status in ("running", "uncertain"):
        return {
            "decision": "retry",
            "reason": "; ".join(checked) or "No unfinished command was launched in the checkpoint",
        }
    return None


def prepare(
    project: Project,
    worktree: Path,
    state: RunRecord,
    directory: Path,
    save: Callable[[], None],
    validate: Callable[[], object],
    *,
    previous: RunRecord | None = None,
    prepared_here: RunRecord | None = None,
    force: bool = False,
) -> bool:
    """Run configured setup commands in order and checkpoint their progress.

    The worktree is revalidated before each command. Return False after a required failure.
    Optional failures are retained as warnings while later steps may continue. Reuse
    durable success only with matching inputs and outputs, or reuse setup just seen
    in this invocation when the project has no persistent cache configured.
    """
    validate()
    prior = previous or {}
    recovery = preparation_recovery(prior, force=force)
    if recovery:
        state["recovery"] = recovery
        if recovery["decision"] == "blocked":
            state.update(status="uncertain", commands=[], error=recovery["reason"])
            save()
            return False
    if not project.preparation:
        state.update(status="skipped", commands=[])
        save()
        return True
    key = preparation_fingerprint(project, worktree)
    outputs = preparation_outputs(project, worktree) if key and not force else None
    try:
        prerequisites_available = not force and all(
            contained_path(worktree, command.cwd).is_dir()
            and not missing_requirements(project, worktree, command, project_requirements=False)
            for command in project.preparation
        )
    except DevAgentError:
        # Let the normal executor persist the failure against the configured command.
        prerequisites_available = False
    if (
        prerequisites_available
        and key
        and outputs
        and prior.get("status") in ("passed", "reused")
        and (prior.get("fingerprint") == key and prior.get("outputs_sha256") == outputs)
    ):
        state.update(
            status="reused",
            commands=[],
            fingerprint=key,
            outputs_sha256=outputs,
            completed_at=prior.get("completed_at"),
            reuse="validated_cache",
        )
        save()

        return True
    if (
        prerequisites_available
        and not project.preparation_cache
        and recovery is None
        and (prepared_here or {}).get("status") in ("passed", "reused")
    ):
        state.update(status="reused", commands=[], reuse="current_invocation")
        save()
        return True
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.update(status="running", commands=[command_record(c) for c in project.preparation])
    save()
    try:
        for command, record in zip(project.preparation, state["commands"], strict=True):
            validate()
            run_command(
                project, worktree, command, record, directory, save, project_requirements=False
            )
            if record.get("failure_kind") == "cleanup_incomplete":
                state.update(status="uncertain", error=record["error"])
                return False
            if record["status"] != "passed" and command.required:
                state["status"] = "failed"
                return False
        state["status"] = (
            "optional_failed"
            if any(c["status"] != "passed" for c in state["commands"])
            else "passed"
        )
        if (
            state["status"] == "passed"
            and key
            and preparation_fingerprint(project, worktree) == key
        ):
            if outputs := preparation_outputs(project, worktree):
                state.update(fingerprint=key, outputs_sha256=outputs)
        return True
    except KeyboardInterrupt:
        state["status"] = (
            "uncertain"
            if any(c.get("failure_kind") == "cleanup_incomplete" for c in state["commands"])
            else "interrupted"
        )
        raise
    except Exception:
        state["status"] = "error"
        raise
    finally:
        state["completed_at"] = now()
        save()
