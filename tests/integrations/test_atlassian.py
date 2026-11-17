"""Protect bounded, sanitised Jira and Confluence retrieval using a synthetic HTTP client."""

import base64
import io
import json
import secrets
import urllib.error
import urllib.parse
from dataclasses import replace

import pytest

from dev_agent.config import AtlassianConfig
from dev_agent.integrations.atlassian import (
    AtlassianProvider,
    NoRedirect,
    RetrievalError,
    content_hash,
)
from tests.support.atlassian import adf, issue
from tests.support.atlassian import atlassian as atlassian


def test_issue_acceptance_structure_revision_and_privacy(atlassian: tuple) -> None:
    """Keep requirement structure and revision while removing unrelated personal metadata."""
    provider, http = atlassian
    snapshot = provider.retrieve("TASK-123")
    assert snapshot["issue"]["description"] == issue()["fields"]["description"]
    assert (
        snapshot["issue"]["acceptance_criteria"]["customfield_10001"]
        == issue()["fields"]["customfield_10001"]
    )
    assert len(snapshot["requirements"]) == 4
    assert snapshot["issue"]["revision"]["updated"] == "2026-01-02T03:04:05.000Z"
    assert snapshot["issue"]["content_sha256"]
    assert "assignee" not in json.dumps(snapshot)
    assert "private-person" not in json.dumps(snapshot)
    requested = next(
        r for r in http.calls if urllib.parse.urlsplit(r.full_url).path.endswith("/TASK-123")
    )
    fields = urllib.parse.parse_qs(urllib.parse.urlsplit(requested.full_url).query)["fields"][0]
    assert "customfield_10001" in fields and "comment" not in fields and "reporter" not in fields
    assert requested.get_header("Authorization").startswith("Basic ")


def test_description_criteria_without_custom_field(atlassian: tuple) -> None:
    """Preserve requirements when acceptance criteria exist only in the description."""
    provider, http = atlassian
    http.responses["/rest/api/3/field"] = []
    http.responses["/rest/api/3/issue/TASK-123"]["fields"]["description"] = adf(
        "Acceptance criteria", "Keep VALUE equal to 1."
    )
    snapshot = provider.retrieve("TASK-123")
    assert snapshot["issue"]["acceptance_criteria"] == {}
    assert any(u["text"] == "Keep VALUE equal to 1." for u in snapshot["requirements"])


def test_each_adf_list_criterion_is_preserved(atlassian: tuple) -> None:
    """Represent each structured checklist item separately in the requirements."""
    provider, http = atlassian
    body = adf()
    body["content"] = [
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": "Acceptance"}],
        },
        {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": adf(text)["content"]}
                for text in ("first", "second")
            ],
        },
    ]
    http.responses["/rest/api/3/issue/TASK-123"]["fields"]["description"] = body
    units = provider.retrieve("TASK-123")["requirements"]
    assert {"first", "second"} <= {u["text"] for u in units}
    assert len({u["requirement_id"] for u in units}) == len(units)
    assert next(u for u in units if u["text"] == "second")["pointer"].endswith(
        "/content/1/content/0"
    )


def test_link_and_search_discovery_deduplicate_and_keep_revision(atlassian: tuple) -> None:
    """Deduplicate discovered pages and retain their revision evidence."""
    provider, http = atlassian
    snapshot = provider.retrieve("TASK-123")
    assert len(snapshot["confluence_documents"]) == 1
    doc = snapshot["confluence_documents"][0]
    assert doc["revision"]["number"] == 3 and doc["parent_id"] == "10"
    assert doc["content_sha256"] == content_hash(
        {k: v for k, v in doc.items() if k not in ("content_sha256", "retrieved_at")}
    )
    assert {r["discovered_by"] for r in snapshot["document_references"]} == {
        "explicit_link",
        "issue_key_search",
    }
    assert sum("/pages/42?" in r.full_url for r in http.calls) == 1


def test_linked_issues_are_supporting_and_bounded(atlassian: tuple) -> None:
    """Keep linked issue content separate from the primary task and apply retrieval caps."""
    provider, http = atlassian
    http.responses["/rest/api/3/issue/TASK-123"]["fields"]["issuelinks"] = [
        {"type": {"name": "Blocks"}, "outwardIssue": {"key": key}} for key in ("TASK-2", "TASK-3")
    ]
    provider.config = replace(provider.config, max_linked_issues=1)
    snapshot = provider.retrieve("TASK-123")
    assert snapshot["linked_issues"][0]["key"] == "TASK-2"
    assert len(snapshot["issue"]["linked_issue_refs"]) == 2
    assert any("capped" in warning for warning in snapshot["warnings"])
    assert "Primary Jira issue" in snapshot["authority"]


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_required_issue_failure_and_no_secret_echo(atlassian: tuple, status: int) -> None:
    """Classify mandatory issue failures without disclosing credentials or raw responses."""
    provider, http = atlassian
    sensitive = provider._secrets[0]
    http.responses["/rest/api/3/issue/TASK-123"] = urllib.error.HTTPError(
        "https://example.atlassian.net", status, sensitive, None, io.BytesIO(sensitive.encode())
    )
    with pytest.raises(RetrievalError) as error:
        provider.retrieve("TASK-123")
    assert sensitive not in str(error.value)
    assert f"HTTP {status}" in str(error.value)
    assert sum("/issue/TASK-123?" in r.full_url for r in http.calls) == 1


def test_optional_document_failure_is_reported(atlassian: tuple) -> None:
    """Retain usable requirements while recording a supporting page omission."""
    provider, http = atlassian
    http.responses["/wiki/api/v2/pages/42"] = urllib.error.HTTPError(
        "https://example.atlassian.net", 403, "private", None, None
    )
    snapshot = provider.retrieve("TASK-123")
    assert snapshot["confluence_documents"] == []
    assert any(
        "Confluence page 42" in warning and "403" in warning for warning in snapshot["warnings"]
    )


def test_missing_auth_and_unsupported_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail early for missing credential variables and requests outside the GET allowlist."""
    monkeypatch.delenv("DEV_AGENT_TEST_TOKEN", raising=False)
    with pytest.raises(RetrievalError, match="credential environment"):
        AtlassianProvider(
            AtlassianConfig(
                "https://example.atlassian.net", "DEV_AGENT_TEST_EMAIL", "DEV_AGENT_TEST_TOKEN"
            )
        )


def test_scoped_gateway_and_separate_product_tokens(
    atlassian: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Address each product gateway with its corresponding test authentication value."""
    provider, http = atlassian
    monkeypatch.setenv("DEV_AGENT_CONFLUENCE_TOKEN", secrets.token_urlsafe(24))
    scoped = AtlassianProvider(
        replace(
            provider.config,
            cloud_id="test-cloud-id",
            confluence_token_env="DEV_AGENT_CONFLUENCE_TOKEN",
        )
    )
    monkeypatch.setattr(scoped._opener, "open", http.open)
    scoped.retrieve("TASK-123")
    assert all(r.full_url.startswith("https://api.atlassian.com/ex/") for r in http.calls)
    jira_auth = next(r.get_header("Authorization") for r in http.calls if "/ex/jira/" in r.full_url)
    confluence_auth = next(
        r.get_header("Authorization") for r in http.calls if "/ex/confluence/" in r.full_url
    )
    assert jira_auth != confluence_auth
    assert base64.b64decode(jira_auth.split()[1]).decode().startswith("operator@example.invalid:")


def test_no_credential_forwarding_to_external_links_or_redirects(atlassian: tuple) -> None:
    """Do not follow arbitrary linked origins or forward the authenticated request on redirect."""
    provider, http = atlassian
    http.responses["/rest/api/3/issue/TASK-123/remotelink"] = [
        {"object": {"url": "https://unrelated.invalid/wiki/spaces/ENG/pages/999/Secret"}},
        {"object": {"url": "http://example.atlassian.net/wiki/spaces/ENG/pages/888/Secret"}},
    ]
    snapshot = provider.retrieve("TASK-123")
    assert all(
        urllib.parse.urlsplit(r.full_url).hostname == "example.atlassian.net" for r in http.calls
    )
    assert not any("/pages/999" in r.full_url or "/pages/888" in r.full_url for r in http.calls)
    assert snapshot["warnings"]
    with pytest.raises(RetrievalError, match="redirect refused"):
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://unrelated.invalid")
    with pytest.raises(RetrievalError, match="allowlist"):
        provider._get("jira", "/rest/api/3/issue/TASK-123/transitions")


def test_credentials_redacted_from_content_and_no_revisions_claimed(atlassian: tuple) -> None:
    """Remove known credential values and report missing revision evidence explicitly."""
    provider, http = atlassian
    sensitive = provider._secrets[0]
    http.responses["/rest/api/3/issue/TASK-123"]["fields"]["description"] = adf(
        "Do not expose " + sensitive
    )
    http.responses["/wiki/api/v2/pages/42"]["version"] = {}
    snapshot = provider.retrieve("TASK-123")
    assert sensitive not in json.dumps(snapshot)
    assert "[REDACTED]" in json.dumps(snapshot)
    assert any("revision unavailable" in w for w in snapshot["warnings"])
    assert any("redacted" in w for w in snapshot["warnings"])


def test_size_limit_and_bad_json_fail_closed(atlassian: tuple) -> None:
    """Reject oversized or malformed primary responses without unsafe diagnostics."""
    provider, http = atlassian
    provider.config = replace(
        provider.config, discover_acceptance_fields=False, max_response_bytes=1024
    )
    http.responses["/rest/api/3/issue/TASK-123"] = b"x" * 1025
    with pytest.raises(RetrievalError, match="size limit"):
        provider.retrieve("TASK-123")
    http.responses["/rest/api/3/issue/TASK-123"] = b"not-json"
    with pytest.raises(RetrievalError, match="JSON response"):
        provider.retrieve("TASK-123")


def test_search_pagination_and_document_caps_are_explicit(atlassian: tuple) -> None:
    """Expose bounded search and page retrieval as recorded omissions."""
    provider, http = atlassian
    http.responses["/wiki/rest/api/search"] = {
        "results": [{"content": {"id": "43"}}],
        "_links": {"next": "/do-not-follow"},
    }
    provider.config = replace(provider.config, max_confluence_pages=1)
    snapshot = provider.retrieve("TASK-123")
    assert snapshot["confluence_documents"][0]["id"] == "42"
    assert any("further pages" in w for w in snapshot["warnings"])
    assert any("not all discovered" in w for w in snapshot["warnings"])


def test_malformed_optional_links_do_not_destroy_primary_requirements(atlassian: tuple) -> None:
    """Keep the primary task usable when optional link data is malformed."""
    provider, http = atlassian
    http.responses["/rest/api/3/issue/TASK-123/remotelink"] = [
        {"object": None},
        {"object": {"url": []}},
        {"object": {"url": "https://[bad/wiki/42"}},
    ]
    http.responses["/wiki/rest/api/search"] = {"results": None, "_links": None}
    snapshot = provider.retrieve("TASK-123")
    assert len(snapshot["requirements"]) == 4
    assert len(snapshot["warnings"]) >= 3


def test_missing_explicit_acceptance_field_is_an_omission(atlassian: tuple) -> None:
    """Report an explicitly configured but unavailable acceptance field."""
    provider, _ = atlassian
    provider.config = replace(provider.config, acceptance_fields=("customfield_99999",))
    snapshot = provider.retrieve("TASK-123")
    assert snapshot["issue"]["acceptance_criteria"]["customfield_99999"] is None
    assert any("customfield_99999 unavailable" in warning for warning in snapshot["warnings"])


def test_content_hash_matches_sanitized_stored_source(atlassian: tuple) -> None:
    """Compute source hashes over the retained sanitised evidence."""
    import os

    provider, http = atlassian
    secret = os.environ["DEV_AGENT_TEST_TOKEN"]
    data = http.responses["/rest/api/3/issue/TASK-123"]
    data["fields"]["description"][secret] = secret
    snapshot = provider.retrieve("TASK-123")
    stored = snapshot["issue"]
    assert secret not in json.dumps(snapshot)
    assert stored["content_sha256"] == content_hash(
        {k: v for k, v in stored.items() if k not in {"retrieved_at", "content_sha256"}}
    )


def test_issue_revision_missing_and_moved_key_are_explicit(atlassian: tuple) -> None:
    """Report a missing revision and reject a primary response with a different issue key."""
    provider, http = atlassian
    data = http.responses["/rest/api/3/issue/TASK-123"]
    data["fields"].pop("updated")
    assert any("revision unavailable" in w for w in provider.retrieve("TASK-123")["warnings"])
    data["key"] = "MOVED-1"
    with pytest.raises(RetrievalError, match="different issue key"):
        provider.retrieve("TASK-123")
