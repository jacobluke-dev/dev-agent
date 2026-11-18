"""Project delivery progress and linked implementation evidence for operators.

Read the child record for check, review, publication and CI status. Persisted planning artifacts
are copied from canonical storage.
"""

import json
from pathlib import Path

from dev_agent.config import Config
from dev_agent.records import RunRecord, Store, atomic_json, atomic_write
from dev_agent.reporting.common import copy_artifact, reserve_results


def summary(config: Config, record: RunRecord) -> RunRecord:
    """Summarise delivery and any retained child implementation or publication evidence."""
    child = {}
    if record.get("implementation_run_id"):
        path = Store(config.runs_root).directory(record["implementation_run_id"]) / "record.json"
        if path.is_file():
            child = Store(config.runs_root).load(record["implementation_run_id"])
    pub = child.get("publication", {})
    return {
        "execution_id": record["run_id"],
        "jira_ticket": record["task"],
        "project": record["project"],
        "pr_mode": record.get("pr_mode", "draft"),
        "verification_policy": record.get("verification_policy", {"mode": "local-full"}),
        "status": record["outcome"],
        "phase": record["phase"],
        "implementation_run_id": record.get("implementation_run_id"),
        "generated_plan": record.get("generated_plan"),
        "verification": [
            {k: a.get(k) for k in ("attempt_id", "profile", "status", "outcome")}
            for a in child.get("verification_attempts", [])
        ],
        "review": child.get("independent_review", {}).get("status", "not_performed"),
        "commit_sha": pub.get("commit_sha"),
        "pr_url": pub.get("pr_url"),
        "draft_pr_url": pub.get("pr_url") if pub.get("draft", True) else None,
        "ci_status": (pub.get("ci_observations") or [{}])[-1].get("status", "not_performed"),
        "blockers": record.get("blockers", []),
        "results_directory": record.get("results_directory"),
    }


def export_delivery(config: Config, record: RunRecord) -> Path:
    """Write the delivery report and copy the available plan and provenance artifacts."""
    directory = reserve_results(config, record)
    canonical = Store(config.runs_root).directory(record["run_id"])
    for name in (
        "implementation-brief.md",
        "delivery-plan.json",
        "requirements-snapshot.json",
        "investigation.json",
        "repository-instructions.json",
        "sources.json",
    ):
        copy_artifact(canonical / name, directory / name)
    for source in canonical.glob("publication-plan-*.json"):
        copy_artifact(source, directory / source.name)
    result = summary(config, record)
    atomic_json(directory / "report.json", record)
    lines = [f"# {record['task']} — Delivery", ""]
    lines += [f"- {key}: `{json.dumps(value, ensure_ascii=True)}`" for key, value in result.items()]
    lines += [
        "",
        "Canonical metadata is authoritative. Publication requires separate confirmation; "
        "no automatic merge or deployment. Missing MCP qualification blocks live delivery.",
        "",
    ]
    atomic_write(directory / "summary.md", "\n".join(lines).encode())
    return directory
