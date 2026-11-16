"""Sequential orchestration, durable phase transitions and human-controlled cleanup."""

import hashlib
from dataclasses import asdict
from pathlib import Path

from dev_agent.agent import AgentRunner, CodexRunner, build_prompt
from dev_agent.config import Config, Project, identifier
from dev_agent.errors import DevAgentError
from dev_agent.git import (
    changed_files,
    check_destination,
    create_worktree,
    git,
    output,
    repository_instructions,
    status,
    validate_repository,
    worktrees,
)
from dev_agent.process import ProcessInterrupted, execute
from dev_agent.records import RunRecord, Store, log, new_id, now


def plan(config: Config, project_name: str, task: str, prompt_file: Path) -> tuple[RunRecord, str]:
    identifier(task, "task identifier")
    project = config.project(project_name)
    baseline = validate_repository(project, config.runs_root)
    prompt_file = prompt_file.expanduser().resolve()
    if prompt_file.suffix.lower() != ".md" or not prompt_file.is_file():
        raise DevAgentError("prompt-file must be an existing Markdown (.md) file")
    try:
        with prompt_file.open("rb") as stream:
            content = stream.read(262145)
        description = content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise DevAgentError(f"Cannot read UTF-8 task prompt: {exc}") from exc
    if not description.strip() or len(content) > 262144 or "\x00" in description:
        raise DevAgentError("Task prompt must contain 1-262144 bytes of UTF-8 Markdown without NUL")
    instructions = repository_instructions(project.repository, baseline.commit)
    run_id = new_id(project.name, task)
    destination = project.worktree_root / run_id
    branch = f"dev-agent/{run_id}"
    check_destination(project, branch, destination)
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "project": project.name,
        "task": task,
        "repository": str(project.repository),
        "git_common_dir": str(baseline.common_dir),
        "base_branch": project.base_branch,
        "baseline_commit": baseline.commit,
        "source_dirty_at_start": baseline.source_dirty,
        "branch": branch,
        "worktree": str(destination),
        "worktree_root": str(project.worktree_root),
        "runs_root": str(config.runs_root),
        "prompt_sha256": hashlib.sha256(content).hexdigest(),
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
        "phase": "planned",
        "outcome": "pending",
        "agent": {
            "provider": "codex",
            "config": asdict(project.agent),
            "status": "pending",
            "exit_code": None,
        },
        "verification": [
            {
                "name": c.name,
                "argv": list(c.argv),
                "timeout_seconds": c.timeout_seconds,
                "status": "not_run",
            }
            for c in project.verification
        ],
        "verification_status": "not_run",
        "implementation_status": "not_run",
        "files_changed": [],
        "files_changed_complete": False,
        "cleanup": None,
        "planned_git_operations": [
            ["git", "worktree", "add", "-b", branch, str(destination), baseline.commit]
        ],
    }
    prompt = build_prompt(
        task, description, instructions, [list(c.argv) for c in project.verification]
    )
    return record, prompt


def implement(
    config: Config,
    project_name: str,
    task: str,
    prompt_file: Path,
    *,
    dry_run: bool = False,
    runner: AgentRunner | None = None,
) -> RunRecord:
    record, prompt = plan(config, project_name, task, prompt_file)
    if dry_run:
        return record
    runner = runner or CodexRunner()
    # Verify the executable/interface before creating ANY run or worktree.
    record["agent"].update(runner.preflight())
    store = Store(config.runs_root)
    directory = store.create(record)
    project = config.project(project_name)
    worktree = Path(record["worktree"])
    with store.lock(record["run_id"]):
        try:
            record.update(started_at=now(), phase="creating_worktree")
            store.save(record)
            create_worktree(project, record["branch"], worktree, record["baseline_commit"])
            log(directory, "Worktree created at exact baseline; source checkout preserved")
            record["phase"] = "agent"
            record["agent"].update(status="running", started_at=now())
            store.save(record)
            result = runner.run(worktree, prompt, directory, project.agent)
            record["agent"].update(
                result.record(), status="passed" if result.ok else "failed", finished_at=now()
            )
            record["implementation_status"] = "agent_completed" if result.ok else "agent_failed"
            store.save(record)
            # The agent can only supply implementation context, never these argv arrays.
            validate_managed_worktree(config, record)
            record["phase"] = "verification"
            store.save(record)
            for check in record["verification"]:
                validate_managed_worktree(config, record)
                check.update(status="running", started_at=now())
                store.save(record)
                result = execute(
                    check["argv"],
                    cwd=worktree,
                    stdout=directory / f"check-{check['name']}.stdout.log",
                    stderr=directory / f"check-{check['name']}.stderr.log",
                    timeout=check["timeout_seconds"],
                )
                check.update(
                    result.record(), status="passed" if result.ok else "failed", finished_at=now()
                )
                log(directory, f"Verification {check['name']}: {check['status']}")
                store.save(record)
            checks = record["verification"]
            record["verification_status"] = (
                "skipped"
                if not checks
                else "passed"
                if all(c["status"] == "passed" for c in checks)
                else "failed"
            )
            record["outcome"] = (
                "agent_failed"
                if record["agent"]["status"] != "passed"
                else "verification_failed"
                if record["verification_status"] == "failed"
                else "unverified"
                if not checks
                else "succeeded"
            )
        except KeyboardInterrupt as exc:
            record.update(outcome="interrupted", error="Execution interrupted; worktree preserved")
            process_result = (
                exc.result.record() if isinstance(exc, ProcessInterrupted) else {"exit_code": None}
            )
            if record["agent"]["status"] == "running":
                record["agent"].update(process_result, status="interrupted", finished_at=now())
                record["implementation_status"] = "interrupted"
            for check in record["verification"]:
                if check["status"] == "running":
                    check.update(process_result, status="interrupted", finished_at=now())
            record["verification_status"] = "incomplete"
        except Exception as exc:
            # Execution boundary: even unexpected adapter/check failures must leave a report.
            record.update(outcome="orchestration_failed", error=str(exc))
            if record["agent"]["status"] == "running":
                record["agent"].update(status="error", error=str(exc), finished_at=now())
                record["implementation_status"] = "error"
            for check in record["verification"]:
                if check["status"] == "running":
                    check.update(status="error", error=str(exc), finished_at=now())
            record["verification_status"] = "incomplete"
            log(directory, f"Orchestration failed during {record['phase']}; see record.json")
        finally:
            try:
                if worktree.exists() or record["outcome"] in ("succeeded", "unverified"):
                    validate_managed_worktree(config, record)
                    record["files_changed"] = changed_files(worktree, record["baseline_commit"])
                    record["files_changed_complete"] = True
            except (DevAgentError, OSError) as exc:
                record["inspection_error"] = str(exc)
                if record["outcome"] in ("succeeded", "unverified"):
                    record["outcome"] = "orchestration_failed"
            record.update(phase="finished", finished_at=now())
            store.save(record)
            log(directory, f"Run finished: {record['outcome']}; worktree retained")
    return record


def validate_managed_worktree(config: Config, record: RunRecord) -> Project:
    project = config.project(record["project"])
    expected = project.worktree_root / record["run_id"]
    if (
        record["repository"] != str(project.repository)
        or record["worktree_root"] != str(project.worktree_root)
        or record["runs_root"] != str(config.runs_root)
        or record["worktree"] != str(expected)
        or record["branch"] != f"dev-agent/{record['run_id']}"
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


def inspect_run(config: Config, run_id: str) -> RunRecord:
    record = Store(config.runs_root).load(run_id)
    if record.get("cleanup"):
        return {"run_id": run_id, "cleanup": record["cleanup"], "branch_retained": record["branch"]}
    validate_managed_worktree(config, record)
    worktree = Path(record["worktree"])
    return {
        "run_id": run_id,
        "outcome": record["outcome"],
        "worktree": str(worktree),
        "branch": record["branch"],
        "baseline_commit": record["baseline_commit"],
        "head_commit": output(worktree, "rev-parse", "HEAD"),
        "dirty_including_ignored": bool(status(worktree, ignored=True)),
        "files_changed": changed_files(worktree, record["baseline_commit"]),
        "diff_stat": output(
            worktree,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--stat",
            record["baseline_commit"],
            "--",
        ),
        "logs": str(config.runs_root / run_id),
    }


def cleanup(config: Config, run_id: str, *, confirmed: bool, force: bool = False) -> RunRecord:
    if not confirmed:
        raise DevAgentError("Cleanup requires explicit confirmation (interactive prompt or --yes)")
    store = Store(config.runs_root)
    with store.lock(run_id):
        record = store.load(run_id)
        if record.get("cleanup"):
            return record
        project = validate_managed_worktree(config, record)
        worktree = Path(record["worktree"])
        if status(worktree, ignored=True) and not force:
            raise DevAgentError(
                "Worktree has uncommitted/untracked/ignored files. Preserve them, "
                "or explicitly use "
                "--force --yes to permanently discard ALL worktree files. The branch is retained."
            )
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        git(project.repository, *args, str(worktree))
        record["cleanup"] = {"at": now(), "forced": force, "branch_retained": True}
        store.save(record)
        log(
            store.directory(run_id),
            "Worktree removed by explicit confirmation; branch and logs retained",
        )
        return record
