"""Render advisory investigation assessment and preserve its evidence in exports.

Receives validated record, requirements snapshots and source evidence from the workflow. The
report distinguishes available sources, citations, limitations and runtime uncertainty.
"""

import html
from pathlib import Path

from dev_agent.config import Config
from dev_agent.records import RunRecord, Store, atomic_json, atomic_write
from dev_agent.reporting.common import compliance_markdown, copy_artifact, reserve_results


def prose(value: object) -> str:
    """Escape evidence text for Markdown prose and table cells."""
    text = html.escape(str(value)).replace("\r", "")
    for character in ("\\", "`", "|", "[", "]"):
        text = text.replace(character, "\\" + character)
    return text


def reference(item: dict) -> str:
    """Format an assessment citation with path, line range, kind and symbol."""
    return (
        f"`{item['path']}:L{item['line_start']}-L{item['line_end']}` "
        f"({item['kind']}; {prose(item['symbol'] or 'no symbol')})"
    )


def investigation_markdown(
    record: RunRecord, snapshot: dict | None, sources: dict, assessment: dict | None
) -> str:
    """Render validated findings or a diagnostic report for an unsuccessful investigation."""
    lines = [f"# {record['task']} — Investigation Report", "", "## Executive summary", ""]
    if assessment:
        lines += [
            prose(assessment["executive_summary"]),
            "",
            "**Static advisory assessment; no tests were executed and no "
            "implementation is approved.**",
        ]
    else:
        lines += [
            f"Investigation did not produce an accepted assessment: **{record['outcome']}**.",
            prose(record.get("error", "No validated investigation result is available.")),
        ]
    lines += ["", compliance_markdown(record)]
    lines += [
        "",
        "## Sources examined",
        "",
        "External/context sources below were retrieved. Repository "
        "citations identify the files cited by the agent; snapshot "
        "availability does not establish examination of every file.",
        "",
    ]
    for source in sources.get("external", []) + sources.get("context", []):
        location = prose(source.get("url", source.get("path", "")))
        revision = prose(source.get("revision", source.get("commit", "unavailable")))
        lines.append(
            f"- `{prose(source['source_id'])}`: {location}; revision `{revision}`; "
            f"SHA-256 `{source.get('content_sha256', 'unavailable')}`; "
            f"{source.get('status', 'retrieved')}."
        )
    lines += [
        "",
        "## Baseline",
        "",
        f"- Branch: `{record['base_branch']}`",
        f"- Exact commit: `{record['baseline_commit']}`",
        "- Remote freshness: **not checked**. No fetch or baseline update occurred.",
        "- Other feature branches: **not investigated**; unmerged work is not baseline behaviour.",
        f"- Source changed during investigation: `{record.get('source_changed', 'unknown')}`.",
        f"- Snapshot changed during investigation: `{record.get('snapshot_changed', 'unknown')}`.",
        "",
        "## Requirements assessment",
        "",
    ]
    if assessment:
        lines += [
            "| Requirement/source unit | Status | Static evidence / test "
            "coverage | Gaps, uncertainties and confidence |",
            "| --- | --- | --- | --- |",
        ]
        for criterion in assessment["criteria"]:
            refs = (
                "; ".join(reference(e) for e in criterion["evidence"] + criterion["tests"])
                or "No repository evidence"
            )
            notes = "; ".join(
                [criterion["explanation"], *criterion["gaps"], *criterion["uncertainties"]]
            )
            lines.append(
                (
                    f"| {prose(criterion['requirement'])} "
                    f"(`{prose(criterion['requirement_id'])}`) | {criterion['status']} | "
                    f"{refs} | {prose(notes)}; confidence: {criterion['confidence']}; "
                    "runtime unverified |"
                ).replace("\n", "<br/>")
            )
    else:
        lines += [
            "Not assessed. Raw agent output, when present, has not passed the "
            "investigation contract."
        ]
    for title, key in (
        ("Existing implementation", "existing_implementation"),
        ("Missing or incomplete functionality", "missing_functionality"),
        ("Architectural constraints", "architectural_constraints"),
        ("Testing assessment", "testing_assessment"),
        ("Dependencies and risks", "dependencies_and_risks"),
        ("Contradictions", "contradictions"),
    ):
        lines += ["", f"## {title}", ""]
        findings = assessment[key] if assessment else []
        for finding in findings:
            citations = finding["source_ids"] + [
                reference(e) for e in finding["repository_evidence"]
            ]
            lines += [f"- {prose(finding['text'])} — {'; '.join(citations)}"]
        if not findings:
            lines.append("No accepted findings recorded for this section.")
        if key == "testing_assessment":
            lines += [
                "",
                "Executed verification: **none**. Test files are coverage "
                "evidence, not passing runtime evidence.",
            ]
    if record.get("limitations"):
        lines += ["", "### Retrieval and context limitations", ""] + [
            f"- {prose(w)}" for w in record["limitations"]
        ]
    lines += ["", "## Autonomous delivery suitability", ""]
    if assessment:
        lines += [
            f"**{assessment['suitability']}** — {prose(assessment['suitability_explanation'])}"
        ]
        lines += [f"- Constraint: {prose(c)}" for c in assessment["constraints"]]
        lines += [
            f"- {f['factor']}: {prose(f['assessment'])} (uncertain: {f['uncertain']})"
            for f in assessment["delivery_factors"]
        ]
    else:
        lines.append("Not assessed; invalid or incomplete output is not a delivery decision.")
    lines += [
        "",
        "## Recommended next action",
        "",
        assessment["recommended_next_action"] if assessment else "Further investigation required.",
        "",
        "Advisory only. No implementation, issue transition, independent "
        "review, branch creation, commit, push, merge or deployment was "
        "performed by this workflow.",
        "Canonical records in runs_root are authoritative. "
        "requirements-snapshot.json preserves the retrieved requirement "
        "structure and sources.json its provenance.",
        "",
    ]
    return "\n".join(lines)


def export_investigation(config: Config, record: RunRecord) -> Path:
    """Export the canonical investigation report, source artifacts and agent output."""
    canonical = Store(config.runs_root).directory(record["run_id"])
    directory = reserve_results(config, record)
    for name in (
        "requirements-snapshot.json",
        "sources.json",
        "context.json",
        "investigation.json",
        "investigation-schema.json",
        "repository-instructions.json",
    ):
        copy_artifact(canonical / name, directory / name)
    for source, target in (
        ("agent-events.jsonl", "agent-events.jsonl"),
        ("final-response.md", "agent-output.md"),
        ("agent.stderr.log", "agent.stderr.log"),
    ):
        copy_artifact(canonical / "agent" / source, directory / target)
    for name in ("agent-events.jsonl", "agent-output.md"):
        if not (directory / name).exists():
            atomic_write(directory / name, b"")
    copy_artifact(canonical / "run.log", directory / "run.log", replace=True)
    # Markdown itself is a canonical generated artifact, then an exported projection.
    copy_artifact(canonical / "investigation.md", directory / "investigation.md", replace=True)
    atomic_json(directory / "report.json", record)
    return directory
