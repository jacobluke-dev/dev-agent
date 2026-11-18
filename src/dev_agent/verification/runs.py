"""Retain verification attempts and qualify their evidence for publication.

Record each check against the configuration digest and source fingerprints before and after
execution. A successful process exit alone is insufficient if source changed or prerequisites
are missing.
"""

import time
from pathlib import Path

from dev_agent.config import Config, Project
from dev_agent.errors import DevAgentError
from dev_agent.records import RunRecord, Store, adoption_id, log, new_id, now
from dev_agent.reporting.common import reserve_results
from dev_agent.reporting.results import save_with_results
from dev_agent.repository.git import changed_files, source_state, validate_managed_worktree
from dev_agent.verification.commands import command_record, prepare, prerequisites, run_command


def verification_attempt(
    config: Config,
    record: RunRecord,
    profile: str | None,
    *,
    initial: bool = False,
    prepare_environment: bool = False,
    automatic_preparation: bool = False,
    prepared_here: RunRecord | None = None,
) -> RunRecord:
    """Append and execute one verification attempt for the current managed worktree.

    Args:
        config: Loaded configuration and expected storage paths.
        record: Canonical implementation record held by the caller under the run lock.
        profile: Check profile name or None for the project default.
        initial: Reuse initial check records and capture the original source fingerprint.
        prepare_environment: Explicitly run setup for this verification attempt.
        automatic_preparation: Ensure setup for an orchestrated check and reject source edits.
        prepared_here: Successful setup from the current workflow invocation, if available.

    Returns:
        The mutable attempt record, including before and after evidence, command outcomes and
            any interruption.
    """
    project = validate_managed_worktree(config, record)
    profile, checks = project.checks(profile)
    worktree = Path(record["worktree"])
    store = Store(config.runs_root)
    directory = store.directory(record["run_id"]) / "verification" / new_id("verify", profile)
    if directory.resolve() != directory:
        raise DevAgentError("Verification storage must not contain symlinks")
    directory.parent.mkdir(exist_ok=True, mode=0o700)
    directory.mkdir(mode=0o700)
    previous = latest_preparation(record)
    attempt = {
        "attempt_id": directory.name,
        "profile": profile,
        "phase": "planned",
        "status": "not_run",
        "outcome": "planned",
        "started_at": now(),
        "finished_at": None,
        "configuration_sha256": config.source_digest,
        "environment_names": sorted(project.env),
        "inherit_env": list(project.inherit_env),
        "checks": record["verification"] if initial else [command_record(c) for c in checks],
        "preparation": {"status": "not_run", "commands": []},
        "changed_since_original": None,
        "source_changed_during_verification": None,
    }
    record.setdefault("verification_attempts", []).append(attempt)

    def save() -> None:
        """Persist the canonical record with the active verification checkpoint."""
        store.save(record)

    def validate() -> Project:
        """Reconfirm worktree identity before inspecting or executing in it."""
        return validate_managed_worktree(config, record)

    started = time.monotonic()
    save()
    try:
        attempt["source_requested"] = source_state(worktree)
        if prepare_environment or automatic_preparation:
            attempt["phase"] = "preparing"
            if not prepare(
                project,
                worktree,
                attempt["preparation"],
                directory / "preparation",
                save,
                validate,
                previous=previous,
                prepared_here=prepared_here,
                force=prepare_environment,
            ):
                missing = any(
                    check["required"] and check.get("failure_kind") == "missing_prerequisite"
                    for check in attempt["preparation"]["commands"]
                )
                attempt.update(
                    status="missing_prerequisite" if missing else "environment_failed",
                    outcome="environment_failed",
                )
                if error := attempt["preparation"].get("error"):
                    attempt["error"] = error
                return attempt
        source = source_state(worktree)
        if (
            automatic_preparation
            and source["fingerprint"] != attempt["source_requested"]["fingerprint"]
        ):
            attempt.update(
                status="preparation_changed_source",
                outcome="environment_failed",
                error="Automatic preparation changed candidate source; review before retrying",
            )
            return attempt
        attempt["prerequisites"] = prerequisites(project, worktree, checks)
        if attempt["prerequisites"]["status"] != "passed":
            attempt.update(status="missing_prerequisite", outcome="environment_failed")
            return attempt
        attempt["source_before"] = source
        attempt["files_changed"] = changed_files(worktree, record["baseline_commit"])
        if initial:
            record["implementation_source"] = attempt["source_before"]
        original = record.get("implementation_source")
        if original:
            attempt["changed_since_original"] = (
                original["fingerprint"] != attempt["source_before"]["fingerprint"]
            )
        else:
            attempt["comparison_note"] = (
                "No original implementation fingerprint recorded; comparison is unknown"
            )
        attempt.update(phase="verifying", status="running")
        save()
        for check, result in zip(checks, attempt["checks"], strict=True):
            validate()
            run_command(project, worktree, check, result, directory, save)
        status_value = (
            "skipped"
            if not checks
            else "passed"
            if all(c["status"] == "passed" for c in attempt["checks"])
            else "failed"
        )
        attempt.update(
            status=status_value,
            outcome={"skipped": "unverified", "passed": "passed", "failed": "verification_failed"}[
                status_value
            ],
        )
        if attempt["outcome"] == "passed" and attempt["preparation"]["status"] == "optional_failed":
            attempt["outcome"] = "passed_with_warnings"
    except KeyboardInterrupt:
        attempt.update(
            status="interrupted", outcome="interrupted", error="Verification interrupted"
        )
    except Exception as exc:
        attempt.update(status="incomplete", outcome="orchestration_failed", error=str(exc))
    finally:
        if attempt["preparation"]["status"] == "running":
            attempt["preparation"]["status"] = (
                "interrupted" if attempt["status"] == "interrupted" else "error"
            )
        try:
            validate()
            attempt["source_after"] = source_state(worktree)
            if "source_before" in attempt:
                attempt["source_changed_during_verification"] = (
                    attempt["source_before"]["fingerprint"]
                    != attempt["source_after"]["fingerprint"]
                )
                if attempt["source_changed_during_verification"] and attempt["status"] == "passed":
                    attempt.update(
                        status="source_changed",
                        outcome="verification_failed",
                        error="Source changed during verification; "
                        "passing checks do not verify a single source state",
                    )
        except (OSError, DevAgentError) as exc:
            attempt["inspection_error"] = str(exc)
            if attempt["outcome"] in ("passed", "passed_with_warnings", "unverified"):
                attempt.update(status="incomplete", outcome="orchestration_failed")
        attempt.update(
            phase="finished", finished_at=now(), duration_seconds=time.monotonic() - started
        )
        save()
        log(
            store.directory(record["run_id"]),
            f"Verification {directory.name}: {attempt['outcome']}",
        )
    return attempt


def verify(
    config: Config,
    run_id: str,
    profile: str | None = None,
    *,
    prepare_environment: bool = False,
    automatic_preparation: bool = False,
    prepared_here: RunRecord | None = None,
) -> RunRecord:
    """Lock a retained implementation run and append a new verification attempt.

    Preserve the original outcome and agent evidence. Refuse cleaned runs and other run kinds.
    Standalone verification requires explicit setup. Orchestrated callers can ensure it.
    """
    store = Store(config.runs_root)
    with store.lock(run_id):
        record = store.load(run_id)
        if record.get("kind") == "adoption" or store.directory(adoption_id(run_id)).exists():
            raise DevAgentError(
                "Use adopt with the original run ID to recheck an adopted candidate"
            )
        if record.get("kind") == "delivery":
            raise DevAgentError("Use the linked implementation_run_id for verification")
        if record.get("kind") == "investigation":
            raise DevAgentError(
                "Investigation records cannot be implementation-verified; start an "
                "explicit implementation separately"
            )
        if record.get("cleanup"):
            raise DevAgentError("Worktree has been cleaned up")
        project = validate_managed_worktree(config, record)
        project.checks(profile)  # Refuse an unknown profile before appending an attempt.
        reserve_results(config, record)
        verification_attempt(
            config,
            record,
            profile,
            prepare_environment=prepare_environment,
            automatic_preparation=automatic_preparation,
            prepared_here=prepared_here,
        )
        save_with_results(config, record, store, preserve_original=True)
        return record


def latest_preparation(record: RunRecord) -> RunRecord | None:
    """Find the latest actual setup state without overlooking a failure or interruption."""
    for attempt in reversed(record.get("verification_attempts", [])):
        state = attempt.get("preparation", {})
        if state.get("status") not in (None, "not_run", "skipped"):
            return state
    return record.get("preparation")


def preparation_blocked(attempt: RunRecord) -> bool:
    """Recognise an attempt stopped before its checks by setup or missing prerequisites."""
    return attempt.get("outcome") == "environment_failed" or attempt.get("preparation", {}).get(
        "status"
    ) in ("running", "interrupted", "error", "failed")


def verification_evidence(
    config: Config, project: Project, record: RunRecord, fingerprint: str
) -> RunRecord:
    """Evaluate targeted and full attempts against the named source fingerprint.

    Compare current matching attempts with configured commands, the config digest,
    prerequisites, completion and source-change evidence. Preserve history and distinguish
    stale, unqualified and never-run evidence. Publication decides whether the result is
    eligible.
    """
    evidence = {"original_outcome": record["outcome"], "profiles": {}}
    for profile in ("targeted", "full"):
        attempts = [
            a for a in record.get("verification_attempts", []) if a.get("profile") == profile
        ]
        current = [
            a
            for a in attempts
            if a.get("source_before", {}).get("fingerprint") == fingerprint
            and a.get("source_after", {}).get("fingerprint") == fingerprint
        ]
        latest = current[-1] if current else None
        status = "not_run" if not attempts else "stale"
        if latest:
            status = latest.get("status", "incomplete")
            configured = project.verification_profiles.get(profile, ())
            expected = [(c.name, list(c.argv), c.cwd) for c in configured]
            actual = [
                (c.get("name"), c.get("argv"), c.get("cwd", ".")) for c in latest.get("checks", [])
            ]
            valid = (
                expected
                and expected == actual
                and latest.get("configuration_sha256") == config.source_digest
                and latest.get("source_changed_during_verification") is False
                and latest.get("prerequisites", {}).get("status") == "passed"
                and latest.get("phase") == "finished"
                and latest.get("outcome") in ("passed", "passed_with_warnings")
                and all(
                    c.get("status") == "passed"
                    and c.get("exit_code") == 0
                    and not c.get("timed_out")
                    for c in latest.get("checks", [])
                )
            )
            if status == "passed" and not valid:
                status = "unqualified"
        evidence["profiles"][profile] = {
            "status": status,
            "attempt_id": latest.get("attempt_id") if latest else None,
            "source_fingerprint": fingerprint,
            "history": [
                {
                    "attempt_id": a.get("attempt_id"),
                    "status": a.get("status"),
                    "source_fingerprint": a.get("source_before", {}).get("fingerprint"),
                }
                for a in attempts
            ],
        }
    return evidence
