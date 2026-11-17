"""Share path validation, artifact copying and Markdown rendering across exporters.

Use canonical run IDs and ownership markers to prevent unrelated result replacement. Exported
evidence complements the authoritative record in runs_root.
"""

import html
import json
import os
import shutil
import uuid
from pathlib import Path

from dev_agent.config import Config, identifier
from dev_agent.errors import DevAgentError
from dev_agent.records import RunRecord, Store, atomic_json


def results_path(config: Config, record: RunRecord) -> Path:
    """Derive the checked task and run export directory from its original results root."""
    identifier(record["task"], "task identifier")
    Store(config.runs_root).directory(record["run_id"])
    root = Path(record.get("results_root", config.results_root))
    if root != config.results_root:
        raise DevAgentError(
            "Restore the run's original results_root; exports are never silently relocated"
        )
    result = root / record["task"] / record["run_id"]
    if result.resolve() != result:
        raise DevAgentError("Results paths must not contain symlinks")
    return result


def reserve_results(config: Config, record: RunRecord) -> Path:
    """Create or validate a private export directory owned by this canonical run."""
    directory = results_path(config, record)
    owner = {
        "run_id": record["run_id"],
        "canonical_record": str(
            Store(config.runs_root).directory(record["run_id"]) / "record.json"
        ),
    }
    if directory.exists():
        marker = directory / ".owner.json"
        if (
            not record.get("results_directory")
            or marker.is_symlink()
            or json.loads(marker.read_text()) != owner
        ):
            raise DevAgentError("Results directory already exists; refusing to overwrite it")
    else:
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(mode=0o700)
        atomic_json(directory / ".owner.json", owner)
        if record.get("kind", "implementation") == "implementation":
            (directory / "preparation").mkdir(mode=0o700)
            (directory / "verification").mkdir(mode=0o700)
    record.update(results_root=str(config.results_root), results_directory=str(directory))
    return directory


def copy_artifact(source: Path, target: Path, *, replace: bool = False) -> None:
    """Atomically export a regular artifact without accepting symlink targets."""
    if target.is_symlink() or target.parent.resolve() != target.parent:
        raise DevAgentError("Refusing a symlink in exported artifacts")
    if target.exists() and not replace:
        return  # Completed evidence is never overwritten by later attempts.
    if not source.is_file() or source.is_symlink():
        return
    temporary = target.parent / f".copy-{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def inline(value: object) -> str:
    """Escape a value for inline Markdown display in a generated report."""
    return "`" + html.escape(str(value)).replace("`", "&#96;").replace("\n", " ") + "`"


def compliance_markdown(record: RunRecord) -> str:
    """Render the recorded instruction status, document coverage and unresolved failures."""
    state = record.get("instruction_compliance")
    if not state:
        return "## Instruction compliance\n\nNot recorded for this historical run.\n"
    docs = state["documents"]
    instructions = [d for d in docs if d["kind"] == "instruction"]
    discovered_count = sum(d.get("discovered", True) for d in instructions)
    supplied_count = sum(d["supplied"] for d in instructions)
    unavailable_count = sum(d["required"] and d["status"] != "loaded" for d in docs)
    architecture_count = sum(d["kind"] == "architecture" and d["supplied"] for d in docs)
    lines = [
        "## Instruction compliance",
        "",
        f"- Availability/scope gate: `{state['status']}`.",
        f"- Discovered instruction scopes: {discovered_count}; supplied: {supplied_count}.",
        f"- Required documents missing, invalid or not loaded: {unavailable_count}.",
        f"- Architectural documents supplied: {architecture_count}.",
        f"- Applicable sets recorded for {len(state['applicable_sets'])} cited/changed paths.",
        "- No dynamic tool interception or verified semantic obedience. See structured provenance.",
    ]
    for failure in state["failures"]:
        text = html.escape(str(failure)).replace("\n", " ")
        for character in ("\\", "`", "[", "]", "|"):
            text = text.replace(character, "\\" + character)
        lines.append("- Failure: " + text)
    return "\n".join(lines) + "\n"
