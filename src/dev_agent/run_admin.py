"""Inspect and explicitly clean up retained runs of different kinds.

Reads canonical records, delegates investigation snapshot handling and validates implementation
worktree identity before using Git. Cleanup preserves evidence and requires operator
confirmation.
"""

from pathlib import Path

from dev_agent.config import Config
from dev_agent.errors import DevAgentError
from dev_agent.investigation.workflow import cleanup_investigation, inspect_investigation
from dev_agent.records import RunRecord, Store, log, now
from dev_agent.reporting.delivery import summary as delivery_summary
from dev_agent.reporting.results import save_with_results
from dev_agent.repository.git import changed_files, git, output, status, validate_managed_worktree


def inspect_run(config: Config, run_id: str) -> RunRecord:
    """Summarise the current state of an implementation, investigation or delivery record.

    Implementation inspection validates a retained worktree before reading its diff. Cleaned
    runs and historical records can be inspected using the evidence they retain.
    """
    record = Store(config.runs_root).load(run_id)
    if record.get("kind") == "delivery":
        return {"kind": "delivery", **delivery_summary(config, record)}
    if record.get("kind") == "investigation":
        return inspect_investigation(config, record)
    latest = (record.get("verification_attempts") or [None])[-1]
    if record["phase"] != "finished":
        current_status = record["phase"]
    elif latest:
        current_status = latest["phase"] if latest["phase"] != "finished" else latest["outcome"]
    else:
        current_status = record["outcome"]
    if record.get("cleanup"):
        return {
            "run_id": run_id,
            "cleanup": record["cleanup"],
            "branch_retained": record["branch"],
            "results_directory": record.get("results_directory"),
            "outcome": record["outcome"],
            "current_status": "cleaned",
            "instruction_compliance": record.get(
                "instruction_compliance", {"status": "not_recorded"}
            ),
        }
    validate_managed_worktree(config, record)
    worktree = Path(record["worktree"])
    return {
        "run_id": run_id,
        "outcome": record["outcome"],
        "phase": record["phase"],
        "instruction_compliance": record.get("instruction_compliance", {"status": "not_recorded"}),
        "publication": record.get("publication"),
        "current_status": current_status,
        "results_directory": record.get("results_directory"),
        "results_error": record.get("results_error"),
        "preparation_status": record.get("preparation", {}).get("status", "not_recorded"),
        "preparation": record.get("preparation"),
        "original_verification": record["verification"],
        "prerequisites": record.get("prerequisites", {"status": "not_recorded"}),
        "verification_status": record["verification_status"],
        "latest_verification": record.get("verification_attempts", [None])[-1]
        if record.get("verification_attempts")
        else None,
        "failure": record.get("error") or record.get("inspection_error"),
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
    """Remove a confirmed run's worktree or investigation snapshot and record the cleanup.

    The run lock protects the operation. Force is required for dirty worktrees or changed or
    unverifiable investigation snapshots. A delivery record must be inspected for its linked
    implementation ID before cleaning that worktree.

    Raises:
        DevAgentError: If confirmation, run identity or cleanup prerequisites are missing.
    """
    if not confirmed:
        raise DevAgentError("Cleanup requires explicit confirmation (interactive prompt or --yes)")
    if Store(config.runs_root).load(run_id).get("kind") == "delivery":
        raise DevAgentError(
            "Delivery evidence is retained; clean up its implementation_run_id explicitly"
        )
    store = Store(config.runs_root)
    with store.lock(run_id):
        record = store.load(run_id)
        if record.get("kind") == "investigation":
            return cleanup_investigation(config, record, force=force)
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
        if record.get("results_directory"):
            save_with_results(config, record, store, preserve_original=True)
        return record
