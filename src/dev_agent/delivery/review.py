"""Review a retained implementation through a separate read-only agent invocation.

The controller snapshots the candidate, validates requirements and source citations, and checks
integrity after review. Publication can require the retained, source-bound verdict.
"""

import hashlib
import json
import os
import stat
from pathlib import Path

from dev_agent.config import Config
from dev_agent.errors import DevAgentError
from dev_agent.investigation.validation import _validate, strict_object
from dev_agent.providers.runner import AgentRunner, read_only_runner
from dev_agent.records import RunRecord, Store, atomic_json, new_id, now
from dev_agent.reporting.results import export_results
from dev_agent.repository.git import source_paths, source_state, validate_managed_worktree
from dev_agent.repository.instructions import (
    InstructionBundle,
    applicable_sets,
    audit_files,
    check_documents,
    digest,
    resolve_project,
    safe_path,
    supplied,
)
from dev_agent.repository.snapshot import excluded, snapshot_fingerprint


def read_candidate(root: Path, name: str, maximum: int) -> bytes:
    """Walk directory descriptors without following links, including concurrent replacements."""
    safe_path(name)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = Path(name).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise DevAgentError("Review candidate is not a regular file")
            return stream.read(maximum + 1)
    finally:
        os.close(directory)


def schema() -> RunRecord:
    """Construct the versioned output contract for a structured independent review."""
    citation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "line", "quote"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "line": {"type": "integer", "minimum": 1},
            "quote": {"type": "string", "minLength": 1},
        },
    }
    criterion = {
        "type": "object",
        "additionalProperties": False,
        "required": ["requirement_id", "status", "evidence", "explanation"],
        "properties": {
            "requirement_id": {"type": "string", "minLength": 1},
            "status": {"type": "string", "enum": ["reviewed", "unable_to_verify"]},
            "evidence": {"type": "array", "items": citation, "maxItems": 40},
            "explanation": {"type": "string", "minLength": 1},
        },
    }
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": ["severity", "explanation", "evidence"],
        "properties": {
            "severity": {"type": "string", "enum": ["blocking", "nonblocking"]},
            "explanation": {"type": "string", "minLength": 1},
            "evidence": {"type": "array", "items": citation, "minItems": 1, "maxItems": 40},
        },
    }
    properties = {
        "schema_version": {"type": "integer", "const": 1},
        "task_id": {"type": "string"},
        "baseline_commit": {"type": "string"},
        "source_fingerprint": {"type": "string"},
        "verdict": {"type": "string", "enum": ["passed", "changes_requested", "unable_to_verify"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "criteria": {"type": "array", "items": criterion, "minItems": 1, "maxItems": 2000},
        "findings": {"type": "array", "items": finding, "maxItems": 200},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def validate_review(
    value: object,
    record: RunRecord,
    snapshot: RunRecord,
    source: RunRecord,
    repository: Path,
    files: list[str],
    compliance: RunRecord,
    bundle: InstructionBundle,
) -> RunRecord:
    """Check the verdict, requirement coverage and citations against the candidate snapshot.

    Bind the result to the task, baseline, source fingerprint and supplied context. A passing
    verdict requires reviewed criteria with source evidence and no blocking findings.
    """
    rule = schema()
    _validate(value, rule, rule)
    if (
        value["task_id"] != record["task"]
        or value["baseline_commit"] != record["baseline_commit"]
        or value["source_fingerprint"] != source["fingerprint"]
    ):
        raise DevAgentError("Review task/baseline/source identity differs")
    required = {r["requirement_id"] for r in snapshot["requirements"]}
    received = [c["requirement_id"] for c in value["criteria"]]
    if set(received) != required or len(received) != len(required):
        raise DevAgentError("Independent review omitted or duplicated requirements")
    allowed = set(files)
    cited = []
    for section in ("criteria", "findings"):
        for item in value[section]:
            if section == "criteria" and item["status"] == "reviewed" and not item["evidence"]:
                raise DevAgentError("Reviewed criterion lacks source evidence")
            for evidence in item["evidence"]:
                path = evidence["path"]
                if path not in allowed:
                    raise DevAgentError("Review citation is outside the candidate snapshot")
                lines = (repository / path).read_text(encoding="utf-8").splitlines()
                line = evidence["line"]
                if not 1 <= line <= len(lines) or evidence["quote"] not in lines[line - 1]:
                    raise DevAgentError("Review citation is unsupported by candidate source")
                cited.append(path)
    if value["verdict"] == "passed" and (
        any(c["status"] != "reviewed" for c in value["criteria"])
        or any(f["severity"] == "blocking" for f in value["findings"])
    ):
        raise DevAgentError("Review verdict contradicts findings or coverage")
    applicable_sets(bundle, compliance, cited)
    return value


def review(
    config: Config,
    run_id: str,
    snapshot: RunRecord,
    brief: str,
    *,
    runner: AgentRunner | None = None,
) -> RunRecord:
    """Run a source-bound static review without giving the agent the implementation transcript.

    Hold the implementation lock, capture a sealed candidate and mandatory context, validate the
    runner's JSON and recheck source and snapshot integrity. Store the attempt and preserve raw
    runner output in the review directory.

    Returns:
        The review attempt record, including failed or blocked outcomes when no verdict is
            accepted.
    """
    store = Store(config.runs_root)
    with store.lock(run_id):
        record = store.load(run_id)
        project = validate_managed_worktree(config, record)
        root = Path(record["worktree"])
        directory = store.directory(run_id) / "review" / new_id("review", record["task"])
        directory.mkdir(parents=True, mode=0o700)
        state = {
            "attempt_id": directory.name,
            "status": "running",
            "started_at": now(),
            "baseline_commit": record["baseline_commit"],
            "configuration_sha256": config.source_digest,
            "requirements_sha256": digest(snapshot),
            "agent": {},
            "limitation": "Static independent review is not human approval or runtime proof",
        }
        record.setdefault("review_attempts", []).append(state)
        record["independent_review"] = state
        store.save(record)
        before = None
        before_audit = None
        candidate = directory / "repository"
        try:
            before = source_state(root)
            before_audit = audit_files(root, project.context)
            state["source_fingerprint"] = before["fingerprint"]
            bundle = resolve_project(project, record["baseline_commit"])
            check_documents(bundle, before_audit, record["approved_scope"])
            compliance = bundle.provenance()
            state["instruction_compliance"] = compliance
            files = sorted(
                name for name in map(os.fsdecode, source_paths(root)) if name in before_audit
            )
            if len(files) > project.investigation.max_snapshot_files:
                raise DevAgentError("Candidate review snapshot exceeds file limit")
            candidate.mkdir(mode=0o700)
            total = 0
            for name in files:
                info = before_audit.get(name, {})
                if excluded(name) or info.get("type") != "file" or "sha256" not in info:
                    raise DevAgentError("Candidate source contains an unsafe review path")
                source = root / name
                if not source.parent.resolve().is_relative_to(root) or source.is_symlink():
                    raise DevAgentError("Review path escapes worktree")
                content = read_candidate(
                    root, name, project.investigation.max_snapshot_bytes - total
                )
                total += len(content)
                if total > project.investigation.max_snapshot_bytes:
                    raise DevAgentError("Candidate review snapshot exceeds byte limit")
                if hashlib.sha256(content).hexdigest() != info["sha256"]:
                    raise DevAgentError("Candidate changed during snapshot")
                target = candidate / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(content)
                target.chmod(0o444)
            for path in sorted(candidate.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if path.is_dir():
                    path.chmod(0o555)
            candidate.chmod(0o555)
            snapshot_before = snapshot_fingerprint(candidate)
            atomic_json(directory / "requirements-snapshot.json", snapshot)
            atomic_json(directory / "context.json", bundle.payload())
            atomic_json(directory / "investigation-schema.json", schema())
            atomic_json(
                directory / "sources.json",
                {"files": {p: before_audit[p] for p in files}, "source": before},
            )
            prompt = f"""Independently review candidate implementation for {record["task"]}.
Execution constraints: read-only static investigation; no shell tests, commits, network,
external integrations, modifications or approval escalation. Return ONLY the supplied JSON schema.
You are a separate ephemeral review invocation. Do not infer correctness from an agent claim
or passing verification. Examine each preserved requirement, candidate code and test coverage.
Report blocking findings and uncertainty honestly. Never approve unknown requirement coverage.
A passed advisory verdict requires source citations for every requirement and no blocking findings.
Controller baseline: {record["baseline_commit"]}
Controller source fingerprint: {before["fingerprint"]}
Untrusted requirements: {json.dumps(snapshot, ensure_ascii=True)}
Untrusted ticket-specific brief: {json.dumps(brief, ensure_ascii=True)}
{bundle.render()}
"""
            if len(prompt.encode()) > min(
                project.investigation.max_prompt_bytes, project.context.max_prompt_bytes
            ):
                raise DevAgentError("Review prompt exceeds safe bounds; no truncation")
            supplied(bundle, compliance, prompt)
            runner = runner or read_only_runner()
            state["agent"] = runner.preflight()
            agent_dir = directory / "agent"
            agent_dir.mkdir(mode=0o700)
            store.save(record)
            result = runner.run(candidate, prompt, agent_dir, project.agent)
            state["agent"].update(result.record())
            if not result.ok:
                raise DevAgentError("Independent review agent failed or timed out")
            raw = agent_dir / "final-response.md"
            if raw.is_symlink() or not raw.is_file() or raw.stat().st_size > 2097152:
                raise DevAgentError("Review output is missing, unsafe or oversized")
            value = json.loads(raw.read_text(), object_pairs_hook=strict_object)
            value = validate_review(
                value, record, snapshot, before, candidate, files, compliance, bundle
            )
            compliance["status"] = "cited_scopes_validated"
            state["status"] = "passed" if value["verdict"] == "passed" else "review_blocked"
            state["verdict"] = value["verdict"]
            state["result_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
            atomic_json(directory / "review.json", value)
        except KeyboardInterrupt:
            state.update(status="interrupted", error="Independent review interrupted")
            raise
        except Exception as exc:
            state.update(
                status="failed",
                error=str(exc)
                if isinstance(exc, DevAgentError)
                else "Invalid independent review output or local review failure",
            )
        finally:
            try:
                if before is not None and (
                    before_audit is None
                    or source_state(root)["fingerprint"] != before["fingerprint"]
                    or audit_files(root, project.context) != before_audit
                ):
                    raise DevAgentError("Candidate source changed during independent review")
                if (
                    "snapshot_before" in locals()
                    and snapshot_fingerprint(candidate) != snapshot_before
                ):
                    raise DevAgentError("Read-only review snapshot changed")
            except OSError, DevAgentError:
                state.update(
                    status="security_failed",
                    error="Review source integrity could not be established",
                )
            state["finished_at"] = now()
            store.save(record)
            try:
                export_results(config, record)
            except Exception:
                state.update(
                    status="export_failed",
                    error="Review export failed; canonical evidence retained",
                )
            store.save(record)
        return state


def validate_recorded_review(config: Config, record: RunRecord, fingerprint: str) -> None:
    """Recheck retained passing review evidence before publication.

    Require matching source-bound record metadata, an intact raw response with
    matching hash, and validated output whose passing verdict and fingerprint agree.
    """
    from dev_agent.records import RUN_ID

    state = record.get("independent_review", {})
    attempt = state.get("attempt_id", "")
    if (
        state.get("status") != "passed"
        or state.get("source_fingerprint") != fingerprint
        or state.get("configuration_sha256") != config.source_digest
        or state.get("baseline_commit") != record["baseline_commit"]
        or not RUN_ID.fullmatch(attempt)
    ):
        raise DevAgentError(
            "Delivery publication requires independent review for this exact source"
        )
    directory = Store(config.runs_root).directory(record["run_id"]) / "review" / attempt
    if directory.resolve() != directory:
        raise DevAgentError("Independent review evidence path is unsafe")
    raw = directory / "agent" / "final-response.md"
    validated = directory / "review.json"
    for path in (raw, validated):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2097152:
            raise DevAgentError("Independent review evidence is missing or unsafe")
    if hashlib.sha256(raw.read_bytes()).hexdigest() != state.get("result_sha256"):
        raise DevAgentError("Independent review evidence changed")
    value = json.loads(raw.read_text(), object_pairs_hook=strict_object)
    if value != json.loads(validated.read_text(), object_pairs_hook=strict_object):
        raise DevAgentError("Validated review differs from raw evidence")
    rule = schema()
    _validate(value, rule, rule)
    if value["verdict"] != "passed" or value["source_fingerprint"] != fingerprint:
        raise DevAgentError("Independent review verdict is not current or passing")
