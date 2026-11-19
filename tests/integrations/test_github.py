"""Test GitHub workflow discovery, exact-commit CI evidence and client credential boundaries."""

import json
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


@pytest.mark.parametrize(
    ("suffix", "endpoint"),
    [
        ("", "repos/example/project"),
        ("compare/abc...def", "repos/example/project/compare/abc...def"),
        (
            "compare/abc...feature%2Ftopic?per_page=1&page=2",
            "repos/example/project/compare/abc...feature%2Ftopic?per_page=1&page=2",
        ),
    ],
)
def test_repository_api_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, endpoint: str
) -> None:
    """Build a metadata URL without a trailing slash and preserve existing suffixes."""
    monkeypatch.setattr("dev_agent.integrations.github.shutil.which", lambda *a, **k: "/fake/gh")

    def request(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Check the exact GET endpoint and return a small response."""
        assert argv == ["/fake/gh", "api", "--hostname", "github.com", "--method", "GET", endpoint]
        return subprocess.CompletedProcess(argv, 0, b'{"ok":true}', b"")

    monkeypatch.setattr(subprocess, "run", request)
    assert GitHub(tmp_path, "example/project").api(suffix) == {"ok": True}


def test_branch_and_pull_requests_encode_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Encode a literal ref appropriately in path and query components."""
    monkeypatch.setattr("dev_agent.integrations.github.shutil.which", lambda *a, **k: "/fake/gh")
    sha = "a" * 40
    pull_query = "state=all&head=example%3Afeature%2Fa%2Bb%23topic%251&per_page=100"
    responses = {
        "repos/example/project/git/ref/heads/feature%2Fa%2Bb%23topic%251": {"object": {"sha": sha}},
        f"repos/example/project/pulls?{pull_query}": [{"number": 7}],
    }

    def request(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Serve responses only for the expected encoded path and query."""
        return subprocess.CompletedProcess(argv, 0, json.dumps(responses[argv[-1]]).encode(), b"")

    monkeypatch.setattr(subprocess, "run", request)
    client = GitHub(tmp_path, "example/project")
    assert client.branch("feature/a+b#topic%1") == sha
    assert client.existing_pull("feature/a+b#topic%1", "main") == {"number": 7}


@pytest.mark.parametrize(
    ("status", "response", "optional", "is_missing"),
    [
        (404, "Not Found", True, True),
        (404, "Not Found", False, False),
        (401, "Unauthenticated", True, False),
        (403, "DO_NOT_EXPORT received upstream HTTP 404", True, False),
        (429, "Rate limited", True, False),
    ],
)
def test_optional_lookup_uses_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    response: str,
    optional: bool,
    is_missing: bool,
) -> None:
    """Treat only a genuine optional 404 as missing and withhold raw diagnostics."""
    monkeypatch.setattr("dev_agent.integrations.github.shutil.which", lambda *a, **k: "/fake/gh")

    def request(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Supply the error format emitted by gh for a failed API request."""
        return subprocess.CompletedProcess(
            argv, 1, b"", f"gh: {response} (HTTP {status})\n".encode()
        )

    monkeypatch.setattr(subprocess, "run", request)
    client = GitHub(tmp_path, "example/project")
    if is_missing:
        assert client.branch("feature/topic", missing=optional) is None
    else:
        with pytest.raises(GitHubError, match="GitHub api failed") as error:
            client.branch("feature/topic", missing=optional)
        assert "DO_NOT_EXPORT" not in str(error.value)


@pytest.mark.parametrize(
    ("login", "name", "permissions", "error"),
    [
        ("operator", "Example/Project", {"push": True}, None),
        ("operator", "other/project", {"push": True}, "does not match"),
        ("operator", "example/project", {"push": False}, "lacks push permission"),
        ("operator", "example/project", {"push": "false"}, "lacks push permission"),
        ("operator", "example/project", None, "lacks push permission"),
        (None, "example/project", {"push": True}, "Cannot establish GitHub"),
        ("operator", None, {"push": True}, "Cannot establish GitHub"),
    ],
)
def test_preflight_checks_identity_and_push_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login: str | None,
    name: str | None,
    permissions: dict[str, object] | None,
    error: str | None,
) -> None:
    """Require usable identity metadata and an explicit true push grant."""
    monkeypatch.setattr("dev_agent.integrations.github.shutil.which", lambda *a, **k: "/fake/gh")

    def request(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Use the real authentication and metadata request shapes."""
        if argv[1:] == ["auth", "status", "--hostname", "github.com"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        responses = {
            "user": {"login": login},
            "repos/example/project": {"full_name": name, "permissions": permissions},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(responses[argv[-1]]).encode(), b"")

    monkeypatch.setattr(subprocess, "run", request)
    client = GitHub(tmp_path, "example/project")
    if error:
        with pytest.raises(GitHubError, match=error):
            client.preflight()
    else:
        assert client.preflight() == login
