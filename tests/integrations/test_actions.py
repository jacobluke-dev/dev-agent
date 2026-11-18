"""Protect workflow qualification and PR-specific required Actions evidence."""

import pytest

from dev_agent.integrations.actions import workflow_problems
from tests.support.publication import CI_WORKFLOW


def test_workflow_accepts_ready_pr_but_rejects_draft_and_wrong_base() -> None:
    """Evaluate the ready-only condition and the selected base independently."""
    assert workflow_problems(CI_WORKFLOW.encode(), "main", "ready", ["Tests"]) == []
    assert "draft PR" in workflow_problems(CI_WORKFLOW.encode(), "main", "draft", ["Tests"])[0]
    assert "v2" in workflow_problems(CI_WORKFLOW.encode(), "v2", "ready", ["Tests"])[0]


@pytest.mark.parametrize(
    "text",
    [
        CI_WORKFLOW.replace("branches: [main]", "paths: [src/**]"),
        CI_WORKFLOW.replace("branches: [main]", "types: [ready_for_review]"),
        CI_WORKFLOW.replace("    runs-on:", "    name: Duplicate\n    runs-on:"),
        CI_WORKFLOW.replace("    runs-on:", "    continue-on-error: true\n    runs-on:"),
        CI_WORKFLOW.replace("- run:", "- if: false\n        run:"),
    ],
)
def test_workflow_fails_closed_when_required_jobs_may_not_run(text: str) -> None:
    """Block filtered events, ambiguous YAML and deliberately skipped checks."""
    assert workflow_problems(text.encode(), "main", "ready", ["Tests"])
