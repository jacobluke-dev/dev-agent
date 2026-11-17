"""Supply synthetic Atlassian payloads and a GET-only HTTP fixture for workflow tests."""

import io
import json
import secrets
import urllib.error
import urllib.parse

import pytest

from dev_agent.config import AtlassianConfig
from dev_agent.integrations.atlassian import (
    AtlassianProvider,
)


def adf(*paragraphs: str) -> dict:
    """Build a synthetic Atlassian Document Format value from paragraphs."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs
        ],
    }


def issue(key: str = "TASK-123") -> dict:
    """Return a fixture Jira issue with requirements, revision and unnecessary identity fields."""
    return {
        "id": "123",
        "key": key,
        "fields": {
            "summary": "Preserve the baseline value",
            "description": adf("Keep VALUE equal to 1.", "Do not change unrelated behaviour."),
            "customfield_10001": adf("Add coverage for the public contract."),
            "issuetype": {"name": "Task"},
            "status": {"name": "Open"},
            "updated": "2026-01-02T03:04:05.000Z",
            "issuelinks": [],
            "assignee": {"emailAddress": "not-requested@example.invalid"},
            "reporter": {"accountId": "not-needed"},
        },
    }


def page(page_id: str = "42") -> dict:
    """Return a fixture Confluence page with a versioned ADF body."""
    return {
        "id": page_id,
        "title": "Architecture",
        "parentId": "10",
        "spaceId": "20",
        "version": {"number": 3, "createdAt": "2026-01-01T00:00:00Z", "authorId": "private-person"},
        "body": {
            "atlas_doc_format": {"value": json.dumps(adf("Keep feature boundaries explicit."))}
        },
        "authorId": "private-person",
    }


class MockHTTP:
    """Provide bounded synthetic HTTP replies and track requests across product endpoints."""

    def __init__(self):
        """Seed response data for the primary task, linked task, fields and pages."""
        self.calls = []
        self.responses = {
            "/rest/api/3/field": [{"id": "customfield_10001", "name": "Acceptance Criteria"}],
            "/rest/api/3/issue/TASK-123": issue(),
            "/rest/api/3/issue/TASK-2": issue("TASK-2"),
            "/rest/api/3/issue/TASK-123/remotelink": [
                {
                    "object": {
                        "url": "https://example.atlassian.net/wiki/spaces/ENG/pages/42/Architecture"
                    }
                }
            ],
            "/wiki/rest/api/search": {"results": [{"content": {"id": "42"}}]},
            "/wiki/api/v2/pages/42": page(),
        }

    def open(self, request: object, timeout: int) -> io.BytesIO:
        """Record one GET request and return or raise the configured synthetic response."""
        self.calls.append(request)
        assert request.get_method() == "GET"
        url = urllib.parse.urlsplit(request.full_url)
        path = url.path
        if path.startswith("/ex/"):
            path = "/" + path.split("/", 4)[4]
        if path not in self.responses:
            raise AssertionError(f"Unexpected read path: {path}")
        value = self.responses[path]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, bytes):
            return io.BytesIO(value)
        return io.BytesIO(json.dumps(value).encode())


@pytest.fixture
def atlassian(monkeypatch: pytest.MonkeyPatch) -> tuple[AtlassianProvider, MockHTTP]:
    """Use ephemeral test credentials and route the provider's requests to in-memory responses."""
    # Generated, test-only values; never use a real token or persist credential fixtures.
    monkeypatch.setenv("DEV_AGENT_TEST_EMAIL", "operator@example.invalid")
    monkeypatch.setenv("DEV_AGENT_TEST_TOKEN", secrets.token_urlsafe(24))
    config = AtlassianConfig(
        "https://example.atlassian.net", "DEV_AGENT_TEST_EMAIL", "DEV_AGENT_TEST_TOKEN"
    )
    provider = AtlassianProvider(config)
    http = MockHTTP()
    monkeypatch.setattr(provider._opener, "open", http.open)
    return provider, http
