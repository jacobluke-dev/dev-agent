"""Test GitHub workflow discovery, exact-commit CI evidence and client credential boundaries."""

import subprocess
from pathlib import Path

import pytest

from dev_agent.integrations.github import GitHub, GitHubError, workflow_triggers
from dev_agent.records import RunRecord


def test_workflow_trigger_inventory() -> None:
    """Recognise supported workflow trigger shapes and flag conditions conservatively."""
    assert workflow_triggers("on: [pull_request, push, workflow_dispatch]\n")["events"] == [
        "pull_request",
        "push",
        "workflow_dispatch",
    ]
    assert workflow_triggers("on:\n  pull_request:\n    types: [opened]\n  push:\njobs:\n")[
        "events"
    ] == ["pull_request", "push"]
    assert workflow_triggers("on: &events {push: {}}\n")["trigger_analysis"] == "unknown"


def test_github_environment_is_not_agent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolate GitHub authentication from the agent subprocess environment."""
    from dev_agent.process import environment

    monkeypatch.setenv("GH_TOKEN", "synthetic-sensitive-placeholder")
    monkeypatch.setattr("dev_agent.integrations.github.shutil.which", lambda *a, **k: "/fake/gh")
    assert GitHub(tmp_path, "example/project").env["GH_TOKEN"] == "synthetic-sensitive-placeholder"
    assert "GH_TOKEN" not in environment()


def test_ci_rejects_wrong_check_commit() -> None:
    """Treat a check run from another SHA as unavailable evidence."""
    from dev_agent.integrations.github import ci_observation

    class ResponseClient:
        """Return an intact PR head with deliberately inconsistent check evidence."""

        def pull(self, number: int) -> RunRecord:
            """Provide the expected PR head for the observation."""
            return {"head": {"sha": "a" * 40}}

        def api(self, suffix: str) -> RunRecord:
            """Supply one check run that names a different commit."""
            return {"check_runs": [{"head_sha": "b" * 40, "name": "Wrong commit"}]}

    assert ci_observation(ResponseClient(), "a" * 40, 1, [])["status"] == "unable_to_retrieve"


def test_monitoring_deadline_is_a_subprocess_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the remaining CI deadline as a stricter timeout for GitHub requests."""
    import time

    monkeypatch.setattr("dev_agent.integrations.github.shutil.which", lambda *a, **k: "/fake/gh")
    client = GitHub(tmp_path, "example/project")
    client.deadline = time.monotonic() + 0.5

    def request(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Assert the allotted timeout and simulate its expiration."""
        assert 0 < kwargs["timeout"] <= 0.5
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", request)
    with pytest.raises(GitHubError, match="could not complete"):
        client.call("api", "user")
