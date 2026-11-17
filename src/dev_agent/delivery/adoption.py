"""Qualify retained legacy changes without rewriting their execution history.

A single linked checkpoint owns fresh source-bound evidence and any later publication. Operator
acceptance of missing historical context is separate from the regular publication confirmation.
"""

import hashlib
import json
import re
from pathlib import Path

from dev_agent.config import Config, Project, identifier
from dev_agent.delivery.review import read_candidate, review, validate_recorded_review
from dev_agent.errors import DevAgentError
from dev_agent.integrations.atlassian import content_hash, requirement_units
from dev_agent.records import RunRecord, Store, adoption_id, now
from dev_agent.reporting.results import save_with_results
from dev_agent.repository.git import (
    changed_files,
    source_state,
    validate_managed_worktree,
    validate_repository,
    validate_unstaged_worktree,
)
from dev_agent.repository.instructions import (
    InstructionBundle,
    approved_scopes,
    audit_files,
    check_documents,
    digest,
    resolve_project,
    validate_changed_paths,
)
from dev_agent.verification.runs import verification_attempt, verification_evidence

IDENTITY_FIELDS = (
    "task",
    "project",
    "repository",
    "git_common_dir",
    "base_branch",
    "baseline_commit",
    "branch",
    "worktree",
    "worktree_root",
    "runs_root",
)


def original_record(store: Store, run_id: str) -> tuple[RunRecord, str]:
    """Load and hash an eligible retained run with absent historical instruction evidence."""
    original = store.load(run_id)
    if (
        original.get("kind", "implementation") != "implementation"
        or original.get("phase") != "finished"
        or original.get("implementation_status") != "agent_completed"
        or original.get("agent", {}).get("exit_code") != 0
        or original.get("cleanup")
        or original.get("delivery_id")
        or original.get("publication")
    ):
        raise DevAgentError("Adoption requires a retained, completed, unpublished implementation")
    history = original.get("instruction_compliance")
    if original.get("outcome") in ("instructions_failed", "security_failed") or (
        history is not None and history != {"status": "not_recorded"}
    ):
        raise DevAgentError(
            "Adoption is only available for missing historical instruction evidence"
        )
    path = store.directory(run_id) / "record.json"
    return original, hashlib.sha256(path.read_bytes()).hexdigest()


def requirements(store: Store, original: RunRecord, requirements_run: str | None) -> RunRecord:
    """Reuse a matching Jira snapshot or the hash-verified original task brief."""
    identifier(original["task"], "task identifier")
    if requirements_run:
        source = store.load(requirements_run)
        if (
            source.get("kind") != "investigation"
            or source.get("outcome") != "investigated"
            or any(source.get(key) != original[key] for key in ("task", "project", "repository"))
        ):
            raise DevAgentError("Requirements run must be a successful investigation of this task")
        data = read_candidate(
            store.directory(requirements_run), "requirements-snapshot.json", 262144
        )
        if len(data) > 262144:
            raise DevAgentError("Requirements snapshot exceeds the adoption input limit")
        snapshot = json.loads(data)
        if (
            snapshot["task_id"] != original["task"]
            or content_hash(snapshot) != source.get("requirements_sha256")
            or requirement_units(snapshot["issue"]) != snapshot["requirements"]
            or not snapshot["requirements"]
        ):
            raise DevAgentError("Requirements snapshot identity or preserved content differs")
        return snapshot | {"provenance": {"kind": "jira_snapshot", "run_id": requirements_run}}
    try:
        raw = read_candidate(Path.cwd(), f"tasks/{original['task']}.md", 262144)
    except FileNotFoundError:
        raise DevAgentError(
            "Original brief not found in tasks/; use --requirements-run for a saved Jira snapshot"
        ) from None
    checksum = hashlib.sha256(raw).hexdigest()
    if checksum != original.get("prompt_sha256"):
        raise DevAgentError(
            "Local task brief differs from original; use --requirements-run instead"
        )
    text = raw.decode("utf-8")
    if len(raw) > 262144 or not text.strip() or "\x00" in text:
        raise DevAgentError("Original task brief is empty or invalid")
    # Preserve prose, lists and Markdown structure for the reviewer.
    return {
        "task_id": original["task"],
        "provenance": {"kind": "hash_matched_original_brief", "sha256": checksum},
        "requirements": [{"requirement_id": "adoption:original-brief", "text": text}],
    }


def audit_candidate(
    project: Project, record: RunRecord, bundle: InstructionBundle, files: RunRecord
) -> RunRecord:
    """Resolve current baseline scopes and check every publishable candidate change."""
    if not project.github:
        raise DevAgentError("Adoption requires configured GitHub publication scope")
    root = Path(record["worktree"])
    scopes = approved_scopes(bundle, project.context.implementation_scope)
    check_documents(bundle, files, scopes)
    changed = changed_files(root, record["baseline_commit"])
    if not changed:
        raise DevAgentError("No implementation changes to adopt")
    validation = validate_changed_paths(bundle, changed, files, scopes, root)
    for name in changed:
        if not any(
            name == path or name.startswith(path + "/") for path in project.github.allowed_paths
        ):
            validation["violations"].append({"path": name, "reason": "outside publication scope"})
    if validation["violations"]:
        raise DevAgentError(f"Current instruction/scope audit failed: {validation['violations']}")
    return {"scope": scopes, "changes": validation, "context": bundle.provenance()}


def check_link(config: Config, record: RunRecord) -> None:
    """Verify the adoption is uniquely linked to the unchanged historical record."""
    source = record["adopted_from"]
    original, checksum = original_record(Store(config.runs_root), source)
    if (
        record["run_id"] != adoption_id(source)
        or checksum != record["adoption"]["original_record_sha256"]
        or any(record.get(key) != original.get(key) for key in IDENTITY_FIELDS)
    ):
        raise DevAgentError("Adoption no longer matches the original execution record")


def approval_digest(config: Config, project: Project, record: RunRecord) -> str:
    """Require passing current checks and review, then bind the approval to their evidence."""
    adoption = record["adoption"]
    candidate = adoption["candidate"]
    fingerprint = candidate["fingerprint"]
    checks = candidate_checks(config, project, record)
    if any(check["status"] != "passed" for check in checks.values()):
        raise DevAgentError("Adoption requires passing current targeted and full verification")
    validate_recorded_review(config, record, fingerprint)
    review_state = record["independent_review"]
    snapshot_hash = digest(adoption["requirements"])
    review_path = f"review/{review_state['attempt_id']}/requirements-snapshot.json"
    raw = read_candidate(Store(config.runs_root).directory(record["run_id"]), review_path, 2097152)
    if (
        len(raw) > 2097152
        or digest(json.loads(raw)) != snapshot_hash
        or review_state.get("requirements_sha256") != snapshot_hash
        or review_state["started_at"] < candidate["started_at"]
    ):
        raise DevAgentError("Adoption review is not current for these requirements")
    return digest(
        {
            "adoption_id": record["run_id"],
            "original": adoption["original_record_sha256"],
            "candidate": candidate,
            "requirements": snapshot_hash,
            "checks": {name: check["attempt_id"] for name, check in checks.items()},
            "review": [review_state["attempt_id"], review_state["result_sha256"]],
        }
    )


def candidate_checks(config: Config, project: Project, record: RunRecord) -> RunRecord:
    """Use only verification attempts started for the current adoption candidate."""
    candidate = record["adoption"]["candidate"]
    current = {
        **record,
        "verification_attempts": [
            attempt
            for attempt in record["verification_attempts"]
            if attempt["started_at"] >= candidate["started_at"]
        ],
    }
    return verification_evidence(config, project, current, candidate["fingerprint"])["profiles"]


def validate_adoption(
    config: Config, project: Project, record: RunRecord, fingerprint: str, audit: RunRecord
) -> RunRecord:
    """Enforce current adoption evidence and explicit acceptance during publication or recovery."""
    check_link(config, record)
    adoption = record["adoption"]
    candidate = adoption["candidate"]
    if (
        adoption.get("status") != "approved"
        or candidate["fingerprint"] != fingerprint
        or candidate["configuration_sha256"] != config.source_digest
        or candidate["audit"] != audit
    ):
        raise DevAgentError("Adoption requires current evidence and explicit legacy approval")
    confirmed = approval_digest(config, project, record)
    if (adoption.get("approvals") or [{}])[-1].get("sha256") != confirmed:
        raise DevAgentError("Legacy approval does not match this candidate and its evidence")
    return {"original_run_id": record["adopted_from"], "legacy_approval_sha256": confirmed}


def adopt(
    config: Config,
    run_id: str,
    *,
    requirements_run: str | None = None,
    prepare: bool = False,
    accept_legacy: str | None = None,
) -> RunRecord:
    """Review an existing legacy worktree or accept one previously qualified candidate.

    The original lock protects the shared worktree. Reuse passing checkpoints for the same
    configuration and source. Preserve prior checks, reviews and approvals across retries.
    """
    if accept_legacy is not None and (
        requirements_run or prepare or not re.fullmatch("[0-9a-f]{64}", accept_legacy)
    ):
        raise DevAgentError(
            "Accept only a printed legacy approval digest, without setup or new requirements"
        )
    store = Store(config.runs_root)
    checkpoint = adoption_id(run_id)
    with store.lock(run_id):
        original, checksum = original_record(store, run_id)
        project = validate_managed_worktree(config, original)
        validate_repository(project, config.runs_root)
        if original["base_branch"] != project.base_branch or not project.github:
            raise DevAgentError(
                "Adoption requires the original configured base and publication policy"
            )
        for profile in ("targeted", "full"):
            if not project.checks(profile)[1]:
                raise DevAgentError(f"Adoption requires configured {profile} verification")
        existing = store.directory(checkpoint).exists()
        if existing:
            record = store.load(checkpoint)
            check_link(config, record)
            if record.get("publication"):
                if requirements_run or prepare or accept_legacy is not None:
                    raise DevAgentError("Publication already started; resume it with publish")
                return record
        elif accept_legacy is not None:
            raise DevAgentError("Review and verify an adoption before accepting legacy provenance")

        root = Path(original["worktree"])
        validate_unstaged_worktree(root)
        before = source_state(root)
        if before["head_commit"] != original["baseline_commit"]:
            raise DevAgentError(
                "Adoption requires the retained branch at its original baseline HEAD"
            )
        bundle = resolve_project(project, original["baseline_commit"])
        audit = audit_candidate(project, original, bundle, audit_files(root, project.context))
        if source_state(root)["fingerprint"] != before["fingerprint"]:
            raise DevAgentError("Source changed during adoption audit")
        candidate = {
            "fingerprint": before["fingerprint"],
            "configuration_sha256": config.source_digest,
            "audit": audit,
        }
        if not existing:
            record = {
                **{key: original[key] for key in IDENTITY_FIELDS},
                "schema_version": 2,
                "kind": "adoption",
                "run_id": checkpoint,
                "adopted_from": run_id,
                "created_at": now(),
                "phase": "finished",
                "outcome": original["outcome"],
                "implementation_status": original["implementation_status"],
                "agent": {"exit_code": 0},
                "cleanup": None,
                "verification": [],
                "verification_status": "not_run",
                "verification_attempts": [],
                "adoption": {
                    "original_record_sha256": checksum,
                    "historical_instruction_enforcement": "not_recorded",
                    "requirements": requirements(store, original, requirements_run),
                },
            }
            store.create(record)
        adoption = record["adoption"]
        snapshot = (
            requirements(store, original, requirements_run)
            if existing and requirements_run
            else adoption["requirements"]
        )
        previous = adoption.get("candidate", {})
        unchanged = {
            key: previous.get(key) for key in candidate
        } == candidate and snapshot == adoption["requirements"]
        if accept_legacy is not None:
            if not unchanged or adoption.get("status") not in (
                "awaiting_legacy_approval",
                "approved",
            ):
                raise DevAgentError(
                    "Candidate approval is stale; run adopt again and review its evidence"
                )
            expected = approval_digest(config, project, record)
            if expected != accept_legacy:
                raise DevAgentError("Legacy approval digest differs from current reviewed evidence")
            approvals = adoption.setdefault("approvals", [])
            if not approvals or approvals[-1]["sha256"] != expected:
                approvals.append({"sha256": expected, "accepted_at": now()})
            adoption["status"] = "approved"
        else:
            if not unchanged:
                adoption.update(candidate={**candidate, "started_at": now()}, requirements=snapshot)
                adoption.pop("approval_sha256", None)
                record["approved_scope"] = audit["scope"]
                record["files_changed"] = audit["changes"]["files"]
            if unchanged and adoption.get("status") == "approved":
                validate_adoption(config, project, record, before["fingerprint"], audit)
                return record
            adoption["status"] = "verifying"
            store.save(record)
            qualify(config, project, record, prepare=prepare)
        save_with_results(config, record, store, preserve_original=True)
        return record


def qualify(config: Config, project: Project, record: RunRecord, *, prepare: bool) -> None:
    """Advance current check and review stages, preserving a resumable checkpoint."""
    store = Store(config.runs_root)
    adoption = record["adoption"]
    candidate = adoption["candidate"]
    source = candidate["fingerprint"]
    root = Path(record["worktree"])
    for profile in ("targeted", "full"):
        check = candidate_checks(config, project, record)[profile]
        if check["status"] != "passed":
            attempt = verification_attempt(config, record, profile, prepare_environment=prepare)
            prepare = False
            if attempt["status"] != "passed" or source_state(root)["fingerprint"] != source:
                adoption.update(status="verification_blocked", blocked_profile=profile)
                return
    adoption.pop("blocked_profile", None)
    adoption["status"] = "reviewing"
    store.save(record)
    try:
        approval_digest(config, project, record)
    except DevAgentError:
        candidate_review = review(
            config,
            record["run_id"],
            adoption["requirements"],
            "Review the retained changes against all acceptance criteria and constraints "
            "in the preserved requirements. Evaluate current instruction compliance and "
            "identify uncertainty; historical instruction enforcement was not recorded.",
        )
        record.update(store.load(record["run_id"]))
        adoption = record["adoption"]
        if candidate_review["status"] != "passed":
            adoption["status"] = "review_blocked"
            return
    bundle = resolve_project(project, record["baseline_commit"])
    if (
        source_state(root)["fingerprint"] != source
        or audit_candidate(project, record, bundle, audit_files(root, project.context))
        != candidate["audit"]
    ):
        raise DevAgentError("Source or instruction scope changed during qualification")
    adoption["approval_sha256"] = approval_digest(config, project, record)
    adoption["status"] = "awaiting_legacy_approval"


def summary(record: RunRecord) -> RunRecord:
    """Show the distinct provenance, audit, checks, review and operator approval states."""
    adoption = record["adoption"]
    return {
        "kind": "adoption",
        "adoption_run_id": record["run_id"],
        "original_run_id": record["adopted_from"],
        "status": adoption.get("status", "planned"),
        "historical_instruction_enforcement": adoption["historical_instruction_enforcement"],
        "candidate": adoption.get("candidate"),
        "requirements_provenance": adoption["requirements"]["provenance"],
        "blocked_profile": adoption.get("blocked_profile"),
        "verification": [
            {key: attempt.get(key) for key in ("attempt_id", "profile", "status", "outcome")}
            for attempt in record["verification_attempts"]
        ],
        "review": record.get("independent_review", {}).get("status", "not_run"),
        "approval_sha256": adoption.get("approval_sha256"),
        "publication": record.get("publication"),
        "results_directory": record.get("results_directory"),
    }
