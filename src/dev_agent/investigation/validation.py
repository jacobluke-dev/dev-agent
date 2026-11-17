"""Validate the packaged investigation schema and ground assessment evidence.

Standalone investigation and delivery use the v1 contract. Review also reuses the schema walker
and duplicate-key rejection. Runtime verification evidence is a separate concern.
"""

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from dev_agent.errors import DevAgentError

FACTORS = {
    "requirements",
    "architecture",
    "implementation",
    "testability",
    "database_migrations",
    "authentication",
    "external_infrastructure",
    "production_risk",
    "cross_feature_dependencies",
    "active_development_conflicts",
}


class InvalidInvestigation(DevAgentError):
    """Reject malformed, inconsistent or unsupported structured assessment evidence."""

    pass


def result_schema() -> dict:
    """Read and decode the investigation-v1 JSON resource from the installed package."""
    return json.loads(
        files("dev_agent.investigation").joinpath("investigation-schema.json").read_text()
    )


def _validate(value: Any, rule: dict, schema: dict, label: str = "$") -> None:
    """Check a value against the supported schema keywords at the given label.

    Resolve local definitions and recursively check object members and array items. Contract
    violations raise InvalidInvestigation with the failing field path.
    """
    if "$ref" in rule:
        _validate(value, schema["$defs"][rule["$ref"].split("/")[-1]], schema, label)
        return
    expected = rule.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if expected and type(value) is not types[expected]:
        raise InvalidInvestigation(f"{label}: expected {expected}")
    if "const" in rule and value != rule["const"]:
        raise InvalidInvestigation(f"{label}: required constant differs")
    if "enum" in rule and value not in rule["enum"]:
        raise InvalidInvestigation(f"{label}: unsupported enum value")
    if isinstance(value, dict):
        if set(rule.get("required", [])) - value.keys():
            raise InvalidInvestigation(f"{label}: required fields missing")
        if rule.get("additionalProperties") is False and value.keys() - rule["properties"].keys():
            raise InvalidInvestigation(f"{label}: unknown fields")
        for key, item in value.items():
            _validate(item, rule["properties"][key], schema, f"{label}.{key}")
    elif isinstance(value, list):
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", 100000):
            raise InvalidInvestigation(f"{label}: invalid array length")
        for index, item in enumerate(value):
            _validate(item, rule["items"], schema, f"{label}[{index}]")
    elif isinstance(value, str):
        if not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 1000000):
            raise InvalidInvestigation(f"{label}: invalid text length")
    elif type(value) is int and value < rule.get("minimum", value):
        raise InvalidInvestigation(f"{label}: number below minimum")


def validate_result(
    value: Any,
    snapshot: dict,
    commit: str,
    repository: Path,
    manifest: list[dict],
    source_ids: set[str],
    limitations: list[str],
) -> dict:
    """Accept an assessment only when it covers the preserved inputs with grounded evidence.

    Validate shape, task and commit identity, one-to-one requirement coverage, supporting source
    IDs, repository citations and suitability constraints. Snapshot omissions and unresolved
    risks limit allowable conclusions. Return the validated value without executing any code.

    Raises:
        InvalidInvestigation: If shape, lineage, citations or suitability are invalid.
    """
    schema = result_schema()
    _validate(value, schema, schema)
    if value["task_id"] != snapshot["task_id"] or value["baseline_commit"] != commit:
        raise InvalidInvestigation("Task/baseline identity differs from this investigation")
    expected = {r["requirement_id"]: r for r in snapshot["requirements"]}
    received = [c["requirement_id"] for c in value["criteria"]]
    if set(received) != expected.keys() or len(received) != len(set(received)):
        raise InvalidInvestigation(
            "Requirement coverage differs from the preserved Jira requirement units"
        )
    allowed = {entry["path"]: entry for entry in manifest if entry["status"] == "materialized"}

    def evidence(item: dict) -> None:
        """Verify a cited file, line range, excerpt and symbol against the materialised baseline."""
        path = item["path"]
        if path not in allowed:
            raise InvalidInvestigation("Citation path is not in the selected baseline snapshot")
        try:
            lines = (repository / path).read_text(encoding="utf-8").splitlines()
        except OSError, UnicodeError:
            raise InvalidInvestigation("Citation is not readable baseline text") from None
        start, end = item["line_start"], item["line_end"]
        if not 1 <= start <= end <= len(lines):
            raise InvalidInvestigation("Citation line range does not exist")
        excerpt = "\n".join(lines[start - 1 : end])
        if item["quote"] not in excerpt or (
            item["symbol"] and item["symbol"] not in "\n".join(lines)
        ):
            raise InvalidInvestigation(
                "Citation quote/symbol is not supported by the baseline file"
            )

    for criterion in value["criteria"]:
        if criterion["requirement"] != expected[criterion["requirement_id"]]["text"]:
            raise InvalidInvestigation(
                "Requirement text changed instead of preserving its source unit"
            )
        for item in criterion["evidence"] + criterion["tests"]:
            evidence(item)
        if any(item["kind"] != "test_coverage" for item in criterion["tests"]):
            raise InvalidInvestigation(
                "Test citations must be labelled as coverage, not executed verification"
            )
        if criterion["status"] == "satisfied" and not any(
            e["kind"] == "static_implementation" for e in criterion["evidence"]
        ):
            raise InvalidInvestigation(
                "Satisfied criterion requires static implementation evidence, not "
                "test existence alone"
            )
        if criterion["status"] == "unable_to_verify" and not criterion["uncertainties"]:
            raise InvalidInvestigation("Unable-to-verify status requires an explicit uncertainty")
        if (
            criterion["status"] in ("not_satisfied", "partially_satisfied")
            and not criterion["gaps"]
        ):
            raise InvalidInvestigation("Unsatisfied criterion requires an explicit gap")
    for section in (
        "existing_implementation",
        "missing_functionality",
        "architectural_constraints",
        "testing_assessment",
        "dependencies_and_risks",
        "contradictions",
    ):
        for finding in value[section]:
            if not finding["source_ids"] and not finding["repository_evidence"]:
                raise InvalidInvestigation("Finding lacks a source citation")
            if any(source not in source_ids for source in finding["source_ids"]):
                raise InvalidInvestigation("Finding cites a source that was not retrieved")
            for item in finding["repository_evidence"]:
                evidence(item)
            if section == "existing_implementation" and not finding["repository_evidence"]:
                raise InvalidInvestigation(
                    "Existing implementation claim lacks repository evidence"
                )
    factors = [f["factor"] for f in value["delivery_factors"]]
    if set(factors) != FACTORS or len(factors) != len(FACTORS):
        raise InvalidInvestigation("Delivery assessment omits or duplicates a required risk factor")
    mapping = {
        "suitable": "Suitable for autonomous implementation.",
        "suitable_with_constraints": "Suitable for autonomous implementation.",
        "requires_human_decision": "Human decision required.",
        "blocked": "Further investigation required.",
        "already_satisfied": "No implementation needed.",
    }
    if value["recommended_next_action"] != mapping[value["suitability"]]:
        raise InvalidInvestigation("Recommended action contradicts the delivery assessment")
    if value["suitability"] == "suitable_with_constraints" and not value["constraints"]:
        raise InvalidInvestigation("Constrained suitability requires explicit constraints")
    if value["suitability"] == "suitable" and (
        any(factor["uncertain"] for factor in value["delivery_factors"])
        or any(c["status"] == "unable_to_verify" for c in value["criteria"])
    ):
        raise InvalidInvestigation("Unqualified suitability contradicts recorded uncertainty")
    if value["suitability"] == "already_satisfied" and any(
        c["status"] not in ("satisfied", "not_applicable") for c in value["criteria"]
    ):
        raise InvalidInvestigation("Already-satisfied assessment contradicts criterion statuses")
    if limitations and value["suitability"] in ("suitable", "already_satisfied"):
        raise InvalidInvestigation(
            "Unqualified suitability/completion is unsupported with retrieval/context omissions"
        )
    return value


def strict_object(pairs: list[tuple[str, object]]) -> dict:
    """Build a decoded JSON object while rejecting duplicate keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInvestigation("Agent JSON contains duplicate object keys")
        result[key] = value
    return result
