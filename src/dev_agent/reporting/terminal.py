"""Render compact operator views over canonical execution and preview evidence.

The renderer only reads saved records and artifact paths. Workflow decisions, validation
and evidence persistence remain with the command handlers and controllers.
"""

import json
import os
import sys
from pathlib import Path
from typing import TextIO

from dev_agent.errors import DevAgentError
from dev_agent.records import RUN_ID, RunRecord, Store

PASSED = {
    "passed",
    "passed_with_warnings",
    "succeeded",
    "delivered",
    "investigated",
    "published",
    "pushed",
    "approved",
    "validated",
    "changed_scopes_validated",
    "cited_scopes_validated",
    "reused",
    "draft_created",
    "ready_pr_created",
    "cleaned",
}
FAILED = {
    "changes_requested",
    "failed",
    "error",
    "interrupted",
    "invalid_investigation",
    "cancelled",
    "timed_out",
}
BLOCKED = {
    "blocked",
    "unverified",
    "mcp_unavailable",
    "ci_not_passed",
    "missing_prerequisite",
    "uncertain",
    "stale",
    "unqualified",
    "incomplete",
    "unable_to_retrieve",
    "unable_to_verify",
    "missing_review_evidence",
    "pr_mismatch",
    "base_mismatch",
    "commit_mismatch",
    "missing",
    "no_checks",
    "no_checks_configured",
    "awaiting_confirmation",
    "awaiting_legacy_approval",
}
PENDING = {
    "pending",
    "planned",
    "running",
    "resolved",
    "supplied",
    "assessed",
    "verified_candidate",
    "verifying",
    "reviewing",
    "committed",
    "staged",
}
NOT_RUN = {"not_run", "not_recorded", "not_performed", "skipped"}

COLOURS = {"PASS": "32", "FAIL": "31", "BLOCKED": "33", "PENDING": "36", "NOT RUN": "90"}


def terminal_text(value: object) -> str:
    """Keep untrusted evidence on one line and neutralise terminal control characters."""
    return "".join(
        c if c.isprintable() else " " if c in "\n\r\t" else ascii(c)[1:-1] for c in str(value)
    )


def label(status: str) -> str:
    """Render a workflow code as a short human-readable label."""
    return status.replace("_", " ")


def state(status: str | None, stream: TextIO) -> str:
    """Show a status with a text indicator and optional terminal colour."""
    detail = label(status or "not recorded")
    if status in PASSED:
        badge = "PASS"
    elif status in FAILED or (status or "").endswith("_failed"):
        badge = "FAIL"
    elif status in BLOCKED or (status or "").endswith("_blocked"):
        badge = "BLOCKED"
    elif status in PENDING:
        badge = "PENDING"
    elif not status or status in NOT_RUN:
        badge = "NOT RUN"
    else:
        return terminal_text(detail)
    tag = f"[{badge}]"
    if stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb":
        tag = f"\033[{COLOURS[badge]}m{tag}\033[0m"
    return f"{tag} {terminal_text(detail)}"


def retained(store: Store | None, run_id: str | None) -> RunRecord:
    """Read optional linked evidence without masking the command's primary result."""
    if store and run_id:
        try:
            return store.load(run_id)
        except DevAgentError, OSError, ValueError:
            pass
    return {}


def record_directory(store: Store, run_id: str | None) -> Path | None:
    """Locate an existing canonical record through the store's run-ID checks."""
    if run_id:
        try:
            directory = store.directory(run_id)
            path = directory / "record.json"
            if path.is_file() and not path.is_symlink():
                return directory
        except DevAgentError, OSError, ValueError:
            pass
    return None


def print_evidence(
    store: Store,
    record: RunRecord,
    stream: TextIO,
    *,
    check: RunRecord | None = None,
) -> None:
    """Point to the exported report and available canonical evidence or failed check logs."""
    directory = record_directory(store, record.get("run_id"))
    if not directory:
        return
    result = record.get("results_directory")
    publication = record.get("publication") or {}
    if result and not (record.get("results_error") or publication.get("export_error")):
        report = Path(result) / "report.json"
        if report.is_file() and not report.is_symlink():
            print(f"Report       {terminal_text(report)}", file=stream)
    print(f"Record       {terminal_text(directory / 'record.json')}", file=stream)
    logs = [check[key] for key in ("stdout_log", "stderr_log") if check and check.get(key)]
    if not logs:
        logs = [
            directory / name
            for name in ("run.log", "agent.stderr.log")
            if (directory / name).is_file()
        ][:1]
    for entry in logs:
        print(f"Log          {terminal_text(entry)}", file=stream)


def first_failed_check(preparation: RunRecord, attempt: RunRecord) -> RunRecord:
    """Select the first recorded failed command in the most relevant attempt."""
    return next(
        (
            check
            for check in (*preparation.get("commands", []), *attempt.get("checks", []))
            if check.get("status") in FAILED
        ),
        {},
    )


def next_action(
    command: str, outcome: str | None, reason: str | None, preview: bool, run: RunRecord
) -> str | None:
    """Offer the supported next step for the recorded blocker or preview."""
    if outcome == "mcp_unavailable":
        return "Review MCP qualification before retrying delivery."
    if outcome == "awaiting_legacy_approval":
        return (
            "Review the adoption evidence, then use adopt ORIGINAL-RUN-ID --accept-legacy DIGEST."
        )
    if outcome == "awaiting_confirmation":
        return "Review the saved publication plan, then resume with --confirm if approved."
    if preview:
        if reason:
            return "Resolve the blocker and repeat the preview."
        if command in ("push", "publish"):
            return "Repeat the preview with --json to review the manifest before confirming."
        return "Review the plan before starting the live command."
    if outcome in {"ci_not_passed", "no_checks", "no_checks_configured", "missing", "pending"} and (
        command in {"ci", "publish"} or outcome == "ci_not_passed"
    ):
        return "Inspect the GitHub checks and use ci RUN-ID --watch to observe again."
    if outcome in {"verification_failed", "verification_blocked"}:
        if run.get("kind") == "adoption":
            return "Review the check logs, then rerun adopt on the original run when ready."
        return "Review the check logs before retrying verification."
    if reason or outcome in FAILED or (outcome or "").endswith(("_failed", "_blocked")):
        return "Inspect the detailed record and logs before retrying."
    if command == "push" and outcome == "pushed":
        return "Preview publish with the desired --pr-mode when ready to open a PR."
    return None


def review_report(store: Store, record: RunRecord, attempt: str) -> tuple[Path | None, RunRecord]:
    """Read only the bounded canonical report for the recorded review attempt."""
    if not isinstance(attempt, str) or not RUN_ID.fullmatch(attempt):
        return None, {}
    directory = record_directory(store, record.get("run_id"))
    if directory is None:
        return None, {}
    path = directory / "review" / attempt / "review.json"
    try:
        if path.resolve() != path or not path.is_file():
            return None, {}
    except OSError:
        return None, {}
    try:
        with path.open("rb") as stream:
            raw = stream.read(2097153)
        if len(raw) <= 2097152:
            data = json.loads(raw)
            if isinstance(data, dict):
                return path, data
    except OSError, ValueError, RecursionError:
        pass
    return path, {}


def review_outcome(review: RunRecord, report: RunRecord, blocked: bool) -> tuple[str, str, str]:
    """Describe the recorded verdict, giving execution and integrity failures precedence."""
    status = review.get("status")
    error = review.get("error")
    if status in FAILED or (status or "").endswith("_failed"):
        return (
            "review_execution_failed" if status == "failed" else status,
            error or "The independent review attempt failed.",
            "Inspect the review attempt and agent logs.",
        )
    verdict = review.get("verdict")
    if status == "review_blocked" and verdict in ("unable_to_verify", "changes_requested"):
        unable = verdict == "unable_to_verify"
        message = (
            "Independent review could not establish correctness."
            if unable
            else "Independent review requested changes."
        )
        if report.get("verdict") == verdict:
            message = report.get("summary") or message
            key, expected = ("status", "unable_to_verify") if unable else ("severity", "blocking")
            entries = report.get("criteria" if unable else "findings")
            if isinstance(entries, list):
                reason = next(
                    (
                        item.get("explanation")
                        for item in entries
                        if isinstance(item, dict)
                        and item.get("explanation")
                        and item.get(key) == expected
                    ),
                    None,
                )
                message = reason or message
        action = (
            "Inspect the review report for the verification limitation."
            if unable
            else "Review the blocking findings before another review."
        )
        return verdict, error or message, action
    if status == "passed" and verdict == "passed":
        return status, "", ""
    if status in ("passed", "review_blocked") or (blocked and not status):
        return (
            "missing_review_evidence",
            "The expected independent review evidence is incomplete.",
            "Inspect the canonical review attempt for the missing evidence.",
        )
    return status or "not_performed", "", ""


def render_run(
    store: Store,
    record: RunRecord,
    command: str,
    *,
    preview: RunRecord | None = None,
    inspection: RunRecord | None = None,
    observation: RunRecord | None = None,
) -> None:
    """Print current lifecycle and recorded stage outcomes with bounded diagnostic detail."""
    stream = sys.stdout
    inspection = inspection or {}
    run = record if preview is None else record | preview
    kind = run.get("kind") or {
        "implement": "implementation",
        "investigate": "investigation",
        "deliver": "delivery",
        "adopt": "adoption",
    }.get(command, "implementation")
    child = retained(store, record.get("implementation_run_id"))
    evidence = child or record
    adoption = evidence.get("adoption") or {}
    attempts = evidence.get("verification_attempts") or []
    attempt = attempts[-1] if attempts else {}
    prep = next(
        (
            a["preparation"]
            for a in reversed(attempts)
            if (a.get("preparation") or {}).get("status") not in (None, "not_run")
        ),
        evidence.get("preparation") or {},
    )
    audit = (adoption.get("candidate") or {}).get("audit", {}).get("changes") or (
        evidence.get("instruction_compliance") or run.get("instructions") or {}
    )
    publication = evidence.get("publication") or {}
    ci = (
        observation if observation is not None else (publication.get("ci_observations") or [{}])[-1]
    )
    phase = record.get("phase")
    if phase == "finished" and kind == "delivery" and record.get("events"):
        phase = record["events"][-1].get("phase") or phase
    outcome = adoption.get("status") or record.get("outcome")
    if kind == "adoption" and adoption.get("status"):
        phase = adoption["status"]
    if command in ("inspect", "runs") and kind == "implementation" and phase == "finished":
        if attempt and attempt.get("phase") != "finished":
            phase, outcome = attempt.get("phase"), attempt.get("status")
        elif publication.get("status"):
            phase = publication.get("failure_phase") or "publication"
            outcome = publication["status"]
        elif attempt:
            phase = f"{attempt.get('profile') or 'recorded'} verification"
            outcome = attempt.get("outcome") or attempt.get("status") or outcome
    if command == "verify" and record.get("results_error"):
        phase, outcome = "exporting evidence", "export_failed"
    elif command == "verify":
        phase, outcome = attempt.get("phase"), attempt.get("outcome")
    elif command == "ci":
        phase, outcome = "observing CI", ci.get("status")
    elif command in ("push", "publish") and preview is None:
        phase = publication.get("failure_phase") or "publication"
        outcome = publication.get("status")
        if outcome == "published" and ci.get("status") != "passed":
            outcome = "ci_not_passed"
    if preview is not None:
        phase = "preview"
        outcome = preview.get("status") or ("blocked" if preview.get("blockers") else "planned")
    if record.get("cleanup"):
        phase = outcome = "cleaned"

    print(
        f"{terminal_text(run.get('project') or 'Project unavailable')} / "
        f"{terminal_text(run.get('task') or 'Ticket unavailable')}",
        file=stream,
    )
    print(
        f"{terminal_text(kind.capitalize())}  {terminal_text(run.get('run_id') or 'preview')}",
        file=stream,
    )
    review_blocked = outcome == "review_blocked"
    review = evidence.get("independent_review") or {}
    report_path, report = review_report(store, evidence, review.get("attempt_id", ""))
    review_status, review_reason, review_action = review_outcome(review, report, review_blocked)
    if review_blocked:
        print("Stage        Independent review", file=stream)
    else:
        print(
            f"Stage        {terminal_text(label(phase or 'not recorded'))}  "
            f"{state(outcome, stream)}",
            file=stream,
        )
    if (run.get("verification_policy") or {}).get("mode"):
        print(f"Policy       {terminal_text(run['verification_policy']['mode'])}", file=stream)

    if preview and "approved_files" not in preview:
        if baseline := preview.get("baseline_commit"):
            print(f"Baseline     {terminal_text(baseline)}", file=stream)
        if profile := preview.get("verification_profile"):
            print(f"Profile      {terminal_text(profile)}", file=stream)

    profiles = {a["profile"]: a.get("status") for a in attempts if a.get("profile")}
    if preview and "approved_files" in preview:
        profiles = {
            name: item.get("status")
            for name, item in preview.get("verification", {}).get("profiles", {}).items()
        }
        audit = preview.get("instruction_validation") or audit
    publication_status = publication.get("status")
    if preview and "approved_files" in preview:
        publication_status = "blocked" if preview.get("blockers") else "planned"
    for name, status in (
        ("Preparation", prep.get("status")),
        ("Instructions", audit.get("status")),
        ("Targeted", profiles.get("targeted")),
        ("Full", profiles.get("full")),
        ("Review", review_status),
        ("Publication", publication_status),
        ("GitHub CI", ci.get("status")),
    ):
        print(f"{name:<13}{state(status or 'not_run', stream)}", file=stream)
    if profiles.get("legacy"):
        print(f"Legacy       {state(profiles['legacy'], stream)}", file=stream)

    for name, value in (
        ("Worktree", inspection.get("worktree") or run.get("worktree") or child.get("worktree")),
        ("Snapshot", inspection.get("snapshot_directory") or run.get("snapshot_directory")),
        ("Review file", report_path),
        ("Child run", record.get("implementation_run_id")),
        ("PR", publication.get("pr_url")),
        ("Commit", publication.get("commit_sha")),
        (
            "Approval",
            adoption.get("approval_sha256") if outcome == "awaiting_legacy_approval" else None,
        ),
        ("Plan digest", preview.get("plan_sha256") if preview else None),
        ("Plan", record.get("publication_plan") or record.get("generated_plan")),
    ):
        if value:
            print(f"{name:<13}{terminal_text(value)}", file=stream)
    if preview and "approved_files" in preview:
        print(
            f"Destination  {terminal_text(preview.get('repository', ''))}"
            f" / {terminal_text(preview.get('branch', ''))}",
            file=stream,
        )
        if parent := preview.get("previous_head") or preview.get("baseline_commit"):
            print(f"Parent       {terminal_text(parent)}", file=stream)
        if command == "publish":
            mode = preview.get("pr_mode") or ("draft" if preview.get("draft") else "ready")
            print(f"PR mode      {terminal_text(mode)}", file=stream)
        print(f"Files        {len(preview['approved_files'])} in proposed manifest", file=stream)

    if not attempt and evidence.get("verification"):
        attempt = {"checks": evidence["verification"]}
    check = first_failed_check(prep, attempt)
    prerequisites = attempt.get("prerequisites") or evidence.get("prerequisites") or {}
    missing = prerequisites.get("failures") or []
    # Report the latest check failure only if this command or the run is in a non-pass state.
    blockers = run.get("blockers") or audit.get("failures") or []
    reason = next(iter(blockers), None) or inspection.get("failure") or record.get("results_error")
    if not reason and command in {"publish", "push", "inspect", "runs"}:
        reason = publication.get("error") or publication.get("export_error")
    if not reason and outcome not in PASSED:
        reason = (
            ci.get("error")
            if command in {"ci", "publish"}
            else record.get("error")
            or attempt.get("error")
            or prep.get("error")
            or next(iter(missing), None)
            or check.get("error")
        )
        if not reason and check:
            reason = f"Check {check.get('name', 'command')} {check['status']}"
        if not reason and (outcome or "").endswith("_blocked"):
            reason = label(outcome)
    if review_blocked and review_reason:
        reason = review_reason
    if reason:
        detail = terminal_text(reason)
        print(f"Blocker      {detail[:280]}{'…' if len(detail) > 280 else ''}", file=stream)
    action = next_action(command, outcome, reason, preview is not None, run)
    if review_blocked and review_action:
        action = (
            review_action
            if report_path or review_status != "unable_to_verify"
            else ("Inspect the canonical review attempt for the verification limitation.")
        )
    if action:
        print(f"Next         {action}", file=stream)
    if record:
        print_evidence(store, record, stream, check=check if reason else None)
        if child and reason:
            print("Implementation evidence", file=stream)
            print_evidence(store, child, stream, check=check)
    elif preview and preview.get("planned_results_directory"):
        print(f"Planned export {terminal_text(preview['planned_results_directory'])}", file=stream)


def render_list(rows: list[RunRecord], columns: tuple[str, ...]) -> None:
    """Print project and run listings as compact, line-oriented text."""
    if not rows:
        print("No runs recorded.")
    for row in rows:
        values = [
            state(row[key], sys.stdout) if key == "outcome" else terminal_text(row[key])
            for key in columns
            if row.get(key) is not None
        ]
        print("  ".join(values))
