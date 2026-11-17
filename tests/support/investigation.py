"""Read-only investigation fixtures for assessment, instruction and delivery tests."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from dev_agent.config import Config, RepositoryContext
from dev_agent.investigation.validation import FACTORS
from dev_agent.process import ProcessResult, execute
from tests.support.atlassian import atlassian as atlassian


def investigation_config(config: Config, provider: object) -> Config:
    """Add fixture Atlassian and repository context settings to the synthetic project."""
    project = replace(
        config.project("example"),
        atlassian=provider.config,
        context=RepositoryContext(instructions=("AGENTS.md",)),
    )
    return replace(config, projects={"example": project})


def accepted_output(snapshot: dict, commit: str) -> dict:
    """Build a schema-valid advisory assessment tied to the fixture baseline and sources."""
    evidence = {
        "path": "hello.py",
        "line_start": 1,
        "line_end": 1,
        "symbol": "VALUE",
        "quote": "VALUE = 1",
        "kind": "static_implementation",
    }
    return {
        "schema_version": 1,
        "task_id": snapshot["task_id"],
        "baseline_commit": commit,
        "executive_summary": "Baseline exposes VALUE; full acceptance needs a human decision.",
        "criteria": [
            {
                "requirement_id": unit["requirement_id"],
                "requirement": unit["text"],
                "status": "unable_to_verify",
                "explanation": "Static source exists; runtime acceptance is unverified.",
                "evidence": [evidence.copy()],
                "tests": [],
                "gaps": [],
                "uncertainties": ["Runtime behaviour was not executed."],
                "confidence": "medium",
                "runtime_verified": False,
            }
            for unit in snapshot["requirements"]
        ],
        "existing_implementation": [
            {
                "text": "VALUE is declared at baseline.",
                "source_ids": [],
                "repository_evidence": [evidence.copy()],
            }
        ],
        "missing_functionality": [],
        "architectural_constraints": [],
        "testing_assessment": [
            {
                "text": "No tests were executed.",
                "source_ids": [snapshot["issue"]["source_id"]],
                "repository_evidence": [],
            }
        ],
        "dependencies_and_risks": [],
        "contradictions": [],
        "delivery_factors": [
            {
                "factor": factor,
                "assessment": "Requires explicit human confirmation.",
                "uncertain": True,
            }
            for factor in sorted(FACTORS)
        ],
        "suitability": "requires_human_decision",
        "suitability_explanation": "Static coverage cannot settle runtime acceptance.",
        "constraints": [],
        "recommended_next_action": "Human decision required.",
        "executed_verification": [],
        "remote_freshness": "not_checked",
        "other_branches_examined": [],
    }


class FakeInvestigator:
    """Run a fake Codex subprocess using prepared output and optional failure injection."""

    def __init__(self, executable: Path):
        """Keep the executable path and initialise response hooks and call tracking."""
        self.executable = executable
        self.output = None
        self.mutate = None
        self.exit_code = 0
        self.called = 0

    def preflight(self) -> dict:
        """Return synthetic read-only adapter provenance without launching a real provider."""
        return {
            "provider": "codex",
            "version": "fake-test",
            "sandbox": "read-only",
            "mode": "investigation",
        }

    def run(self, repository: Path, prompt: str, directory: Path, config: object) -> ProcessResult:
        """Check fixture inputs and run the fake assessment process with real logs."""
        self.called += 1
        assert "STATIC READ-ONLY INVESTIGATION" in prompt
        assert "Keep VALUE equal to 1." in prompt
        assert not (repository / ".git").exists()
        assert (repository / "hello.py").read_text() == "VALUE = 1\n"
        assert (repository / "hello.py").stat().st_mode & 0o222 == 0
        snapshot = json.loads((directory.parent / "requirements-snapshot.json").read_text())
        commit = json.loads((directory.parent / "sources.json").read_text())["baseline_commit"]
        value = accepted_output(snapshot, commit)
        if callable(self.output):
            value = self.output(value)
        elif self.output is not None:
            value = self.output
        raw = value if isinstance(value, str) else json.dumps(value)
        fixture = directory / "fake-input.json"
        fixture.write_text(raw)
        if self.mutate:
            self.mutate(repository)
        return execute(
            [
                sys.executable,
                str(self.executable),
                "--sandbox",
                "read-only",
                str(fixture),
                str(directory / "final-response.md"),
                str(self.exit_code),
            ],
            cwd=repository,
            stdout=directory / "agent-events.jsonl",
            stderr=directory / "agent.stderr.log",
            timeout=10,
            stdin=prompt,
        )


@pytest.fixture
def investigator(tmp_path: Path) -> FakeInvestigator:
    """Write a fake read-only Codex executable for reproducible assessment tests."""
    executable = tmp_path / "fake-codex.py"
    executable.write_text("""import json, os, pathlib, sys
assert sys.argv[1:3] == ['--sandbox', 'read-only']
assert 'DEV_AGENT_TEST_TOKEN' not in os.environ
assert 'DEV_AGENT_TEST_EMAIL' not in os.environ
prompt = sys.stdin.read()
assert 'UNTRUSTED DATA' in prompt
pathlib.Path(sys.argv[4]).write_text(pathlib.Path(sys.argv[3]).read_text())
print(json.dumps({'type':'turn.completed'}))
print('fake diagnostic', file=sys.stderr)
sys.exit(int(sys.argv[5]))
""")
    return FakeInvestigator(executable)
