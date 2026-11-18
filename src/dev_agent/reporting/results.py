"""Dispatch result exports and render implementation summaries.

Copy evidence and add presentation links to a projection rather than the canonical record.
Persist export failures so the run store still tells operators what happened.
"""

import json
from pathlib import Path

from dev_agent.config import Config, identifier
from dev_agent.errors import DevAgentError
from dev_agent.records import RunRecord, Store, atomic_json, atomic_write
from dev_agent.reporting.common import compliance_markdown, copy_artifact, inline, reserve_results
from dev_agent.reporting.delivery import export_delivery
from dev_agent.reporting.investigation import export_investigation


def summary(record: RunRecord, directory: Path) -> str:
    """Render implementation, verification, review and publication evidence as Markdown."""
    lines = [f"# {record['task']} — Execution Results", "", "## Overview", ""]
    for label, key in (
        ("Project", "project"),
        ("Task", "task"),
        ("Run ID", "run_id"),
        ("Baseline", "base_branch"),
        ("Baseline commit", "baseline_commit"),
        ("Worktree", "worktree"),
    ):
        lines.append(f"- {label}: {inline(record[key])}")
    lines += [
        "",
        "## Execution",
        "",
        "- Environment preparation: "
        f"{inline(record.get('preparation', {}).get('status', 'not_recorded'))}",
        f"- Codex implementation: {inline(record.get('implementation_status', 'not_run'))}",
        f"- Original verification profile: {inline(record.get('verification_profile', 'legacy'))}",
        f"- Prerequisites: {inline(record.get('prerequisites', {}).get('status', 'not_recorded'))}",
        f"- Original outcome: {inline(record['outcome'])}",
    ]
    if record.get("delivery_id"):
        lines += [
            f"- Delivery execution: {inline(record['delivery_id'])}",
            "- Independent static review: "
            + inline(record.get("independent_review", {}).get("status", "not_performed")),
        ]
    if record.get("kind") == "adoption":
        adoption = record["adoption"]
        lines += [
            "",
            "## Legacy adoption",
            "",
            f"- Original run: {inline(record['adopted_from'])}",
            "- Historical instruction enforcement: unrecorded",
            "- Current instruction and scope audit: "
            + inline(
                adoption.get("candidate", {})
                .get("audit", {})
                .get("changes", {})
                .get("status", "not_run")
            ),
            "- Independent static review: "
            + inline(record.get("independent_review", {}).get("status", "not_run")),
            f"- Legacy provenance status: {inline(adoption.get('status', 'planned'))}",
            f"- Approval digest: {inline(adoption.get('approval_sha256'))}",
        ]
    attempts = record.get("verification_attempts", [])
    for profile in ("targeted", "full", "legacy"):
        selected = [a for a in attempts if a["profile"] == profile]
        if selected:
            latest = selected[-1]
            lines.append(
                f"- {profile.capitalize()} verification: {inline(latest['status'])} "
                f"(attempt {inline(latest['attempt_id'])})"
            )
        elif profile != "legacy" or record.get("schema_version") == 1:
            status = (
                record.get("verification_status", "not_run") if profile == "legacy" else "not_run"
            )
            lines.append(f"- {profile.capitalize()} verification: {inline(status)}")
    if attempts:
        latest = attempts[-1]
        lines.append(
            f"- Latest verification outcome: {inline(latest.get('outcome', 'incomplete'))}; "
            "applies only to its recorded source state"
        )
    lines += [
        "",
        compliance_markdown(record),
        "",
        "## Changes",
        "",
        "Audited candidate changed files:"
        if record.get("kind") == "adoption"
        else "Original run changed files:",
        "",
    ]
    lines += [f"- {inline(name)}" for name in record.get("files_changed", [])] or [
        "- No changed files recorded."
    ]
    output = directory / "agent-output.md"
    if output.exists():
        with output.open(encoding="utf-8", errors="replace") as stream:
            text = stream.read(2048)
        first = next(
            (line.strip().lstrip("# ") for line in text.splitlines() if line.strip()),
            "No agent output recorded.",
        )
        lines += [
            "",
            f"Agent-reported note (unreviewed): {inline(first[:400])}",
            "",
            "[Complete agent output](agent-output.md)",
        ]
    lines += ["", "## Preparation", ""]
    failures = []
    groups = [("Original preparation", record.get("preparation", {}).get("commands", []))]
    for prior in record.get("preparation_history", []):
        groups.append((f"Earlier preparation {prior['attempt_id']}", prior["state"]["commands"]))
    for attempt in attempts:
        commands = attempt.get("preparation", {}).get("commands", [])
        if commands:
            groups.append((f"Preparation for {attempt['attempt_id']}", commands))

    def describe_checks(title: str, checks: list[RunRecord]) -> None:
        """Append a check group's timing and output links and collect its failures."""
        lines.extend([f"### {title}", ""])
        if not checks:
            lines.extend(["No commands performed.", ""])
        for check in checks:
            duration = check.get("duration_seconds")
            duration_text = f"{duration:.3f}" if duration is not None else "not_run"
            links = []
            for kind in ("stdout", "stderr"):
                relative = check.get(f"export_{kind}")
                if relative:
                    links.append(f"[{kind}]({relative})")
            lines.append(
                f"- {inline(check['name'])}: {inline(check['status'])}; "
                f"duration {inline(duration_text)} s; "
                f"exit {inline(check.get('exit_code'))}. " + " ".join(links)
            )
            if check["status"] not in ("passed", "not_run"):
                failures.append(
                    f"{check['name']}: {check.get('failure_kind', check['status'])}; "
                    f"{check.get('error') or 'See captured output; cause not inferred.'}"
                )
        lines.append("")

    for title, checks in groups:
        describe_checks(title, checks)
    lines += ["## Verification", ""]
    if not attempts or record.get("schema_version") == 1:
        describe_checks("Original verification", record.get("verification", []))
    for attempt in attempts:
        describe_checks(f"{attempt['profile']} / {attempt['attempt_id']}", attempt["checks"])
        state = attempt.get("source_before", {})
        lines.append(
            f"- {inline(attempt['attempt_id'])}: "
            f"HEAD {inline(state.get('head_commit', 'unknown'))}; "
            f"fingerprint {inline(state.get('fingerprint', 'unknown'))}; "
            f"changed since original: {inline(attempt.get('changed_since_original', 'unknown'))}; "
            "changed during verification: "
            f"{inline(attempt.get('source_changed_during_verification', 'unknown'))}."
        )
        failures += attempt.get("prerequisites", {}).get("failures", [])
        if attempt.get("error"):
            failures.append(attempt["error"])
        if attempt.get("comparison_note"):
            lines.append(f"- Source comparison: {inline(attempt['comparison_note'])}")
        if preparation := attempt.get("preparation"):
            lines.append(f"- Preparation: {inline(preparation['status'])}")
    failures += record.get("prerequisites", {}).get("failures", [])
    for key in ("error", "inspection_error", "results_error"):
        if record.get(key):
            failures.append(record[key])
    if record.get("agent", {}).get("status") not in ("passed", "pending", None):
        failures.append(
            "Agent execution did not complete successfully; see report.json and agent logs."
        )
    lines += ["", "## Failures", ""] + (
        [f"- {inline(f)}" for f in failures]
        or ["No failures recorded. Skipped checks are not passes."]
    )
    publication = record.get("publication")
    if publication:
        lines += ["", "## GitHub publication", ""]
        for label, value in (
            ("Status", publication.get("status")),
            ("Repository", publication.get("repository")),
            ("Published commit", publication.get("commit_sha")),
            ("PR mode", publication.get("pr_mode", "draft")),
            ("Pull request", publication.get("pr_url")),
            (
                "Verification policy",
                publication.get("approved_plan", {})
                .get("verification_policy", {})
                .get("mode", "local-full"),
            ),
            ("Latest CI", (publication.get("ci_observations") or [{}])[-1].get("status")),
            (
                "Incomplete full verification acknowledgement",
                publication.get("approved_plan", {}).get("incomplete_full_reason"),
            ),
            (
                "Recovery",
                publication.get("recovery") if publication.get("status") == "failed" else None,
            ),
        ):
            if value:
                lines.append(f"- {label}: {inline(value)}")
        lines.append(
            "CI observations certify only their exact recorded commit. Merging remains manual."
        )
    lines += [
        "",
        "## Human Review",
        "",
        "The implementation has not been approved. "
        + (
            "GitHub publication was separately human-authorised; see its evidence above. "
            "No automatic merge or deployment was performed."
            if publication
            else "The orchestrator did not commit, push, merge or deploy."
        ),
        "Any created worktree remains available for inspection until explicit cleanup. "
        "Trusted commands and human changes must be reviewed separately.",
        "",
        "Canonical metadata is stored in runs_root; this directory is an exported projection.",
        "",
    ]
    return "\n".join(lines)


def export_results(config: Config, record: RunRecord) -> Path:
    """Export a run according to its kind and return its validated results directory.

    Reserve or validate ownership of the destination. Copy logs and retained review or
    publication evidence. Add artifact links to a projected record for display while canonical
    paths remain authoritative.
    """
    if record.get("kind") == "delivery":
        return export_delivery(config, record)
    if record.get("kind") == "investigation":
        return export_investigation(config, record)
    directory = reserve_results(config, record)
    canonical = Store(config.runs_root).directory(record["run_id"])
    for source in (canonical / "review").glob("*/*"):
        if source.name not in (
            "review.json",
            "sources.json",
            "context.json",
            "investigation-schema.json",
        ):
            continue
        if source.is_symlink() or source.parent.resolve() != source.parent:
            raise DevAgentError("Review evidence contains a symlink")
        target = directory / "review" / source.parent.name / source.name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        copy_artifact(source, target)
    publication_root = canonical / "publication"
    if publication_root.exists():
        if publication_root.resolve() != publication_root:
            raise DevAgentError("Publication evidence must not contain symlinks")
        for source in publication_root.glob("*/*"):
            if source.name not in ("plan.json", "approved-files.json", "pr-body.md"):
                continue
            if source.parent.is_symlink():
                raise DevAgentError("Publication evidence must not contain symlinks")
            target = directory / "publication" / source.parent.name / source.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            copy_artifact(source, target)
    for name in (
        "repository-instructions.json",
        "instruction-audit-before.json",
        "instruction-audit-after.json",
    ):
        copy_artifact(canonical / name, directory / name)
    for source, target in (
        ("agent-events.jsonl", "agent-events.jsonl"),
        ("final-response.md", "agent-output.md"),
        ("agent.stderr.log", "agent.stderr.log"),
        ("run.log", "run.log"),
    ):
        copy_artifact(canonical / source, directory / target, replace=source == "run.log")
    for name in ("agent-events.jsonl", "agent-output.md"):
        if not (directory / name).exists():
            atomic_write(directory / name, b"")
    # Add export links to a copy; canonical evidence does not contain presentation paths.
    projection = json.loads(json.dumps(record))
    setup = projection.get("preparation", {})
    setup_directory = "preparation"
    if attempt_id := setup.get("attempt_id"):
        identifier(attempt_id, "preparation attempt ID")
        setup_directory += f"/{attempt_id}"
    groups = [(setup_directory, setup.get("commands", []))]
    for prior in projection.get("preparation_history", []):
        identifier(prior["attempt_id"], "preparation attempt ID")
        groups.append((f"preparation/history/{prior['attempt_id']}", prior["state"]["commands"]))
    if not projection.get("verification_attempts") or projection.get("schema_version") == 1:
        groups.append(("verification/original", projection.get("verification", [])))
    for attempt in projection.get("verification_attempts", []):
        identifier(attempt["attempt_id"], "verification attempt ID")
        groups += [
            (f"verification/{attempt['attempt_id']}", attempt["checks"]),
            (
                f"preparation/{attempt['attempt_id']}",
                attempt.get("preparation", {}).get("commands", []),
            ),
        ]
    for relative, checks in groups:
        destination = directory / relative
        if destination.resolve() != destination:
            raise DevAgentError("Export directory contains symlinks")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for check in checks:
            identifier(check["name"], "check name")
            for kind in ("stdout", "stderr"):
                source = Path(
                    check.get(f"{kind}_log", canonical / f"check-{check['name']}.{kind}.log")
                )
                if not source.resolve().is_relative_to(canonical):
                    raise DevAgentError("Artifact source is outside canonical run storage")
                target = destination / source.name
                copy_artifact(source, target)
                if target.exists():
                    check[f"export_{kind}"] = str(target.relative_to(directory))
    for attempt in projection.get("verification_attempts", []):
        target = directory / "verification" / attempt["attempt_id"] / "report.json"
        if not target.exists():
            atomic_json(target, attempt)
    atomic_json(directory / "report.json", projection)
    atomic_write(directory / "summary.md", summary(projection, directory).encode())
    return directory


def save_with_results(
    config: Config, record: RunRecord, store: Store, *, preserve_original: bool = False
) -> None:
    """Save canonical evidence before attempting to update the results export.

    Record export failures in the run. The preserve_original option keeps an established outcome
    when adding verification or cleanup evidence.
    """
    store.save(record)
    try:
        record.pop("results_error", None)
        export_results(config, record)
    except Exception as exc:
        record["results_error"] = str(exc)
        if not preserve_original and record["outcome"] in (
            "passed",
            "passed_with_warnings",
            "unverified",
        ):
            record["outcome"] = "orchestration_failed"
    store.save(record)
