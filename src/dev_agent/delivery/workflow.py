"""Coordinate Jira delivery and resume its steps using canonical checkpoints.

The MCP intake gate is currently unqualified and stops live Jira delivery before implementation.
The downstream workflow connects existing planning, implementation, verification, review, draft
publication and CI machinery.
"""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from dev_agent.config import Config
from dev_agent.delivery.implementation import implement
from dev_agent.delivery.publication import ci_run, publish_run
from dev_agent.delivery.review import review
from dev_agent.errors import DevAgentError
from dev_agent.integrations.atlassian import content_hash, jira_key, requirement_units
from dev_agent.investigation.validation import validate_result
from dev_agent.records import RunRecord, Store, atomic_json, atomic_write, new_id, now
from dev_agent.reporting.delivery import export_delivery
from dev_agent.repository.git import source_state, validate_managed_worktree, validate_repository
from dev_agent.repository.instructions import InstructionBundle, resolve_project
from dev_agent.repository.snapshot import materialize
from dev_agent.verification.runs import verify

MCP_BLOCKER = (
    "Codex Atlassian MCP delivery is unavailable: the isolated catalogue reported notLoggedIn; "
    "a qualified OAuth/transport and source-provenance adapter is not implemented. "
    "No REST fallback, new Jira credentials or sandbox relaxation is permitted. "
    "See docs/mcp-qualification.md."
)


class DeliveryBlocked(DevAgentError):
    """Carry the status and reason for a safe stop in the delivery lifecycle."""

    def __init__(self, status: str, message: str) -> None:
        """Attach the blocker outcome used in the canonical delivery record."""
        super().__init__(message)
        self.status = status


def mcp_intake(
    config: Config, record: RunRecord, directory: Path, bundle: InstructionBundle
) -> RunRecord:
    """Stop Jira delivery until isolated Atlassian MCP intake is qualified.

    No CLI or configuration option bypasses the gate. Synthetic intake evidence in tests
    exercises downstream orchestration. The standalone REST investigation provider is not used
    as a delivery fallback.
    """
    raise DeliveryBlocked("mcp_unavailable", MCP_BLOCKER)


def brief(snapshot: RunRecord, assessment: RunRecord) -> str:
    """Assemble a ticket-specific Markdown brief from validated requirements and findings.

    Keep requirement IDs, source citations, architecture and constraints. The implementation
    runner receives its instruction bundle separately.
    """
    lines = [f"# {snapshot['task_id']} — {snapshot['issue']['title']}", "", "## Requirements", ""]
    lines += [f"- [{r['requirement_id']}] {r['text']}" for r in snapshot["requirements"]]
    lines += [
        "",
        "## Existing behaviour and implementation plan",
        "",
        assessment["executive_summary"],
        "",
    ]
    for criterion in assessment["criteria"]:
        lines += [
            f"- {criterion['requirement_id']}: {criterion['status']}. {criterion['explanation']}"
        ]
        lines += [f"  Gap: {g}" for g in criterion["gaps"]]
        lines += [f"  Uncertainty: {u}" for u in criterion["uncertainties"]]
        lines += [
            f"  Source: {e['path']}:{e['line_start']}-{e['line_end']} ({e['symbol']})"
            for e in criterion["evidence"] + criterion["tests"]
        ]
    for title, key in [
        ("Architecture", "architectural_constraints"),
        ("Dependencies and decisions", "dependencies_and_risks"),
        ("Conflicts", "contradictions"),
        ("Testing expectations", "testing_assessment"),
    ]:
        lines += ["", f"## {title}", ""]
        for finding in assessment[key]:
            refs = finding["source_ids"] + [
                f"{e['path']}:{e['line_start']}" for e in finding["repository_evidence"]
            ]
            lines.append(f"- {finding['text']} (sources: {', '.join(refs)})")
    lines += ["", "## Delivery constraints", ""] + [f"- {c}" for c in assessment["constraints"]]
    lines += [
        "",
        "Applicable engineering instructions are supplied separately by dev-agent. "
        "This brief does not replace or duplicate the scoped AGENTS.md hierarchy.",
        "",
    ]
    return "\n".join(lines)


def validate_intake(
    config: Config, record: RunRecord, value: RunRecord, directory: Path
) -> tuple[RunRecord, RunRecord]:
    """Check a candidate MCP requirements snapshot and assessment against the exact baseline.

    Validate source identity, requirement coverage and hashes. Materialise a sealed baseline in
    the delivery directory and verify the assessment structure and citations against it.
    Nonactionable suitability stops delivery with a recorded reason.
    """
    if value.get("provider") != "codex_atlassian_mcp":
        raise DeliveryBlocked(
            "invalid_intake", "Delivery accepts only qualified Codex Atlassian MCP evidence"
        )
    snapshot = value["requirements_snapshot"]
    if snapshot["task_id"] != record["task"] or snapshot.get("provider") != "codex_atlassian_mcp":
        raise DeliveryBlocked(
            "invalid_intake", "Jira/MCP source identity differs from selected ticket"
        )
    issue = snapshot["issue"]
    if (
        not issue.get("source_id")
        or not issue.get("retrieved_at")
        or not issue.get("url", "").startswith("https://")
    ):
        raise DeliveryBlocked("invalid_intake", "Required Jira provenance is missing")
    if requirement_units(issue) != snapshot["requirements"]:
        raise DeliveryBlocked(
            "invalid_intake", "Requirement units do not preserve original Jira content"
        )
    sources = [issue, *snapshot["linked_issues"], *snapshot["confluence_documents"]]
    for source in sources:
        expected = content_hash(
            {k: v for k, v in source.items() if k not in ("retrieved_at", "content_sha256")}
        )
        if source.get("content_sha256") != expected:
            raise DeliveryBlocked("invalid_intake", "Source hash does not match preserved content")
    project = config.project(record["project"])
    repository = directory / "planning-repository"
    manifest, warnings = materialize(project, record["baseline_commit"], repository)
    bundle = resolve_project(project, record["baseline_commit"])
    ids = {s["source_id"] for s in sources}
    ids |= {d["source_id"] for d in bundle.documents if d.get("status") == "loaded"}
    ids |= {f"repository:{e['path']}" for e in manifest if e["status"] == "materialized"}
    assessment = validate_result(
        value["assessment"],
        snapshot,
        record["baseline_commit"],
        repository,
        manifest,
        ids,
        snapshot["warnings"] + warnings,
    )
    if assessment["suitability"] not in (
        "suitable",
        "suitable_with_constraints",
        "already_satisfied",
    ):
        raise DeliveryBlocked(
            "human_decision_required",
            "Ticket assessment requires a human decision; --confirm only authorises publication",
        )
    return snapshot, assessment


def _validate_resume(config: Config, record: RunRecord, project_name: str, task: str) -> None:
    """Check the saved delivery's identity, configuration, baseline and planning artifact hashes."""
    if (
        record.get("kind") != "delivery"
        or record["project"] != project_name
        or record["task"] != task
        or record["runs_root"] != str(config.runs_root)
        or record["configuration_sha256"] != config.source_digest
    ):
        raise DevAgentError("Delivery identity/configuration changed; refusing unsafe resumption")
    project = config.project(project_name)
    baseline = validate_repository(project, config.runs_root)
    if (
        str(project.repository) != record["repository"]
        or baseline.commit != record["baseline_commit"]
    ):
        raise DevAgentError(
            "Delivery repository or selected baseline changed; start a new execution"
        )
    for name, checksum in record.get("artifact_hashes", {}).items():
        if name not in {
            "implementation-brief.md",
            "delivery-plan.json",
            "requirements-snapshot.json",
            "investigation.json",
            "repository-instructions.json",
            "sources.json",
        }:
            raise DevAgentError("Unrecognised delivery artifact")
        path = Store(config.runs_root).directory(record["run_id"]) / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise DevAgentError(
                "Delivery artifact changed; refusing to replace historical evidence"
            )


def deliver(
    config: Config,
    project_name: str,
    task: str,
    *,
    confirmed: bool = False,
    watch: bool = False,
    dry_run: bool = False,
    resume: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> RunRecord:
    """Run or resume the delivery workflow and retain its checkpoint evidence.

    Dry-run validates local configuration and baseline context only. A live run records the
    intake blocker or advances an eligible checkpoint through implementation, both verification
    profiles, independent review, publication preview and CI observation. Confirm authorises
    only the commit, push and draft PR step after mandatory gates.

    Returns:
        A local dry-run plan or the canonical delivery record with its outcome and blockers.
    """
    jira_key(task)
    project = config.project(project_name)
    baseline = validate_repository(project, config.runs_root)
    bundle = resolve_project(project, baseline.commit)
    for profile in ("targeted", "full"):
        if not project.checks(profile)[1]:
            raise DevAgentError(f"Delivery requires configured {profile} verification")
    if not project.github:
        raise DevAgentError("Delivery requires trusted GitHub publication configuration")
    if dry_run:
        if resume:
            _validate_resume(config, Store(config.runs_root).load(resume), project_name, task)
        return {
            "dry_run": True,
            "project": project_name,
            "task": task,
            "baseline_commit": baseline.commit,
            "instructions": bundle.provenance(),
            "status": "mcp_unavailable",
            "blockers": [MCP_BLOCKER],
            "confirm_scope": "Commit, push and draft PR only; never merge/deploy",
            "results_root": str(config.results_root),
            "canonical_root": str(config.runs_root),
        }
    store = Store(config.runs_root)
    if resume:
        record = store.load(resume)
        _validate_resume(config, record, project_name, task)
    else:
        record = {
            "schema_version": 2,
            "kind": "delivery",
            "run_id": new_id(project_name, task),
            "project": project_name,
            "task": task,
            "repository": str(project.repository),
            "base_branch": project.base_branch,
            "baseline_commit": baseline.commit,
            "runs_root": str(config.runs_root),
            "configuration_sha256": config.source_digest,
            "created_at": now(),
            "phase": "planned",
            "outcome": "planned",
            "cleanup": None,
            "instruction_compliance": bundle.provenance(),
            "events": [],
            "blockers": [],
            "artifact_hashes": {},
            "implementation_run_id": None,
            "generated_plan": None,
        }
        store.create(record)
    directory = store.directory(record["run_id"])
    with store.lock(record["run_id"]):
        # Reload under lock; another invocation must not overwrite a later checkpoint.
        record = store.load(record["run_id"])
        _validate_resume(config, record, project_name, task)

        def save() -> None:
            """Write the canonical checkpoint, export its projection and retain the results path."""
            store.save(record)
            export_delivery(config, record)
            store.save(record)

        def stage(value: str) -> None:
            """Persist a phase transition and emit it to the optional progress callback."""
            record.update(phase=value, outcome="running", blockers=[])
            record["events"].append({"phase": value, "at": now()})
            save()
            if progress:
                progress(value)

        try:
            record.setdefault("authorisations", []).append(
                {"at": now(), "publication_confirmed": confirmed, "watch": watch}
            )
            if not record.get("generated_plan"):
                if (directory / "planning-repository").exists():
                    raise DeliveryBlocked(
                        "planning_incomplete",
                        "Partial planning evidence exists; inspect canonical artifacts. "
                        "No overwrite or automatic restart",
                    )
                stage("retrieving_mcp_requirements")
                intake = mcp_intake(config, record, directory, bundle)
                snapshot, assessment = validate_intake(config, record, intake, directory)
                text = brief(snapshot, assessment)
                if len(text.encode()) > 262144:
                    raise DeliveryBlocked(
                        "invalid_intake", "Generated brief exceeds implementation prompt bound"
                    )
                artifacts = {
                    "requirements-snapshot.json": snapshot,
                    "investigation.json": assessment,
                    "repository-instructions.json": bundle.payload(),
                    "sources.json": {
                        "provider": "codex_atlassian_mcp",
                        "baseline_commit": baseline.commit,
                        "external": [
                            snapshot["issue"],
                            *snapshot["linked_issues"],
                            *snapshot["confluence_documents"],
                        ],
                    },
                    "delivery-plan.json": {
                        "schema_version": 1,
                        "task": task,
                        "baseline_commit": baseline.commit,
                        "suitability": assessment["suitability"],
                        "brief": "implementation-brief.md",
                        "publication_requires_confirmation": True,
                        "independent_review_required": True,
                    },
                }
                for name, data in artifacts.items():
                    atomic_json(directory / name, data)
                atomic_write(directory / "implementation-brief.md", text.encode())
                record["artifact_hashes"] = {
                    name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                    for name in [*artifacts, "implementation-brief.md"]
                }
                record["generated_plan"] = str(directory / "implementation-brief.md")
                record["suitability"] = assessment["suitability"]
                save()
            if record["suitability"] == "already_satisfied":
                raise DeliveryBlocked(
                    "already_satisfied",
                    "Baseline reports no implementation needed; human review remains required",
                )
            snapshot = json.loads((directory / "requirements-snapshot.json").read_text())
            if not record["implementation_run_id"]:
                record["implementation_run_id"] = new_id(project_name, task)
                stage("implementation_reserved")
            child_id = record["implementation_run_id"]
            child_dir = store.directory(child_id)
            if not child_dir.exists():
                stage("implementing")
                child = implement(
                    config,
                    project_name,
                    task,
                    directory / "implementation-brief.md",
                    verification="targeted",
                    execution_id=child_id,
                    delivery_id=record["run_id"],
                    expected_baseline=baseline.commit,
                    progress=progress,
                )
            else:
                child = store.load(child_id)
            if (
                child.get("delivery_id") != record["run_id"]
                or child.get("task") != task
                or child.get("baseline_commit") != baseline.commit
            ):
                raise DeliveryBlocked(
                    "integrity_failed", "Implementation checkpoint identity differs"
                )
            validate_managed_worktree(config, child)
            if (
                child.get("phase") != "finished"
                or child.get("implementation_status") != "agent_completed"
            ):
                raise DeliveryBlocked(
                    "implementation_blocked",
                    "Implementation incomplete/failed; inspect its run ID. "
                    "No automatic agent restart",
                )
            if child.get("instruction_compliance", {}).get("status") != "changed_scopes_validated":
                raise DeliveryBlocked(
                    "instructions_failed", "Implementation instruction enforcement did not pass"
                )
            if not child.get("publication", {}).get("commit_sha"):
                for profile in ("targeted", "full"):
                    attempts = [
                        a
                        for a in child.get("verification_attempts", [])
                        if a.get("profile") == profile
                    ]
                    if not attempts:
                        stage("verifying_" + profile)
                        child = verify(config, child_id, profile)
                        attempts = [
                            a for a in child["verification_attempts"] if a["profile"] == profile
                        ]
                    latest = attempts[-1]
                    fingerprint = source_state(Path(child["worktree"]))["fingerprint"]
                    if (
                        latest.get("status") != "passed"
                        or latest.get("source_after", {}).get("fingerprint") != fingerprint
                    ):
                        raise DeliveryBlocked(
                            "verification_blocked",
                            "Current " + profile + " verification has not passed; "
                            "verify its implementation run, then resume",
                        )
            current_review = child.get("independent_review", {})
            fingerprint = (
                child.get("publication", {}).get("approved_plan", {}).get("verified_fingerprint")
                or source_state(Path(child["worktree"]))["fingerprint"]
            )
            if (
                current_review.get("status") != "passed"
                or current_review.get("source_fingerprint") != fingerprint
            ):
                if current_review.get("source_fingerprint") == fingerprint and current_review.get(
                    "status"
                ) not in (None, "running", "interrupted"):
                    raise DeliveryBlocked(
                        "review_blocked",
                        "Existing review has failures/findings; "
                        "unchanged source is not automatically re-reviewed",
                    )
                stage("independent_review")
                current_review = review(
                    config, child_id, snapshot, (directory / "implementation-brief.md").read_text()
                )
                if current_review["status"] != "passed":
                    raise DeliveryBlocked(
                        "review_blocked", "Independent review did not pass; inspect review evidence"
                    )
            child = store.load(child_id)
            if child.get("publication", {}).get("pr_number"):
                if child["publication"].get("status") == "published":
                    stage("monitoring_ci")
                    observation = ci_run(config, child_id, watch=watch)
                    record["outcome"] = (
                        "delivered" if observation["status"] == "passed" else "ci_not_passed"
                    )
                    record["blockers"] = (
                        []
                        if observation["status"] == "passed"
                        else [f"CI is {observation['status']}; no success claimed"]
                    )
                    return record
                stage("recovering_publication")
            stage("publication_preview")
            options = {
                "title": snapshot["issue"]["title"],
                "summary": json.loads((directory / "investigation.json").read_text())[
                    "executive_summary"
                ][:2000],
            }
            preview = publish_run(config, child_id, dry_run=True, **options)["plan"]
            path = directory / f"publication-plan-{new_id('preview', task)}.json"
            atomic_json(path, preview)
            record["publication_plan"] = str(path)
            if preview["blockers"]:
                raise DeliveryBlocked("publication_blocked", "; ".join(preview["blockers"]))
            if not confirmed:
                record.update(
                    outcome="awaiting_confirmation",
                    blockers=["Use --resume with --confirm after reviewing publication plan"],
                )
                return record
            stage("publishing_draft")
            child = publish_run(
                config,
                child_id,
                confirmed=True,
                watch=watch,
                plan_sha256=preview["plan_sha256"],
                **options,
            )
            pub = child["publication"]
            if pub["status"] != "published":
                raise DeliveryBlocked(
                    "publication_failed",
                    "Publication failed; retain commit and resume recovery; no automatic retry",
                )
            ci = (pub.get("ci_observations") or [{}])[-1].get("status", "not_performed")
            record.update(
                outcome="delivered" if ci == "passed" else "ci_not_passed",
                blockers=[] if ci == "passed" else [f"CI is {ci}; no success claimed"],
            )
        except DeliveryBlocked as exc:
            record.update(outcome=exc.status, blockers=[str(exc)])
        except KeyboardInterrupt:
            record.update(
                outcome="interrupted",
                blockers=[
                    "Interrupted; resume this execution ID; no automatic implementation restart"
                ],
            )
        except Exception as exc:
            record.update(
                outcome="orchestration_failed",
                blockers=[
                    str(exc)
                    if isinstance(exc, DevAgentError)
                    else "Delivery step failed; inspect canonical evidence"
                ],
            )
        finally:
            record["phase"] = "finished"
            record["finished_at"] = now()
            save()
        return record
