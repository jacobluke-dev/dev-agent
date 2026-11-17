"""Retrieve bounded Jira and Confluence evidence for standalone investigation.

The REST provider uses allowlisted GET endpoints, refuses redirects and sanitises source data
before it enters records or prompts. It is distinct from the gated Atlassian MCP intake used by
delivery.
"""

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from dev_agent.config import AtlassianConfig
from dev_agent.errors import DevAgentError
from dev_agent.records import now

JIRA_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,31}-[1-9][0-9]{0,15}\Z")
type Document = dict[str, Any]


class RetrievalError(DevAgentError):
    """Safe diagnostic containing no response body, request headers or credentials."""


class TaskProvider(Protocol):
    """Specify a source retrieval and sanitisation interface for investigations."""

    def retrieve(self, task: str) -> Document:
        """Return a sanitised requirements snapshot for a validated task key."""
        ...

    def sanitize(self, text: str) -> str:
        """Remove provider-known secrets from text before export or model input."""
        ...


def jira_key(value: str) -> str:
    """Validate an uppercase Jira issue key before using it in an API path."""
    if not JIRA_KEY.fullmatch(value):
        raise DevAgentError("Investigation requires an uppercase Jira issue key such as TASK-123")
    return value


def content_hash(value: object) -> str:
    """Hash a deterministic JSON representation of retained source evidence."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent urllib from following redirects with a request carrying credentials."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Refuse redirect handling instead of constructing a follow-up request."""
        raise RetrievalError("Atlassian redirect refused; verify the configured site/cloud ID")


class AtlassianProvider:
    """Own the REST credentials, transport and warning state for issue retrieval.

    Each retrieval resets warnings and redaction state. The provider returns source snapshots
    rather than request or authentication objects.
    """

    def __init__(self, config: AtlassianConfig):
        """Load configured credential variables and construct a redirect-refusing opener.

        Missing credentials raise RetrievalError. Secrets remain in provider state for
        authentication and sanitisation.
        """
        self.config = config
        names = [
            config.email_env,
            config.token_env,
            config.confluence_token_env or config.token_env,
        ]
        values = []
        for name in names:
            value = os.environ.get(name, "")
            if not value or any(ord(c) < 32 for c in value):
                raise RetrievalError(
                    f"Missing or invalid Atlassian credential environment variable: {name}"
                )
            values.append(value)
        email, jira_token, confluence_token = values
        self._auth = {
            "jira": base64.b64encode(f"{email}:{jira_token}".encode()).decode(),
            "confluence": base64.b64encode(f"{email}:{confluence_token}".encode()).decode(),
        }
        self._secrets = sorted(set(values + list(self._auth.values())), key=len, reverse=True)
        # No proxy inheritance or redirects that could forward the Authorization header.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.warnings: list[str] = []
        self._redacted = False

    def sanitize(self, text: str) -> str:
        """Replace known secrets, including URL-encoded and JSON-escaped forms."""
        for secret in self._secrets:
            for variant in (secret, urllib.parse.quote(secret, safe=""), json.dumps(secret)[1:-1]):
                text = text.replace(variant, "[REDACTED]")
        return text

    def _clean(self, value: Any) -> Any:
        """Recursively redact known credentials and omit unnecessary identity fields."""
        if isinstance(value, str):
            clean = self.sanitize(value)
            self._redacted |= clean != value
            return clean
        if isinstance(value, list):
            return [self._clean(v) for v in value]
        if isinstance(value, dict):
            if value.get("type") == "mention" and isinstance(value.get("attrs"), dict):
                value = value | {"attrs": {k: v for k, v in value["attrs"].items() if k != "id"}}
            # Avoid unnecessary identity metadata, including ADF mention IDs.
            return {
                self._clean(k): self._clean(v)
                for k, v in value.items()
                if k
                not in {
                    "accountId",
                    "emailAddress",
                    "avatarUrls",
                    "authorId",
                    "ownerId",
                    "lastOwnerId",
                }
                and not (k == "id" and value.get("type") == "mention")
            }
        return value

    def _get(self, product: str, path: str, params: dict[str, object] | None = None) -> Any:
        """Perform one bounded GET against an allowlisted product endpoint.

        Resolve the configured origin or product gateway, apply product credentials, cap and
        decode the response, then sanitise it. Transport, authorization and response failures
        raise RetrievalError without returning raw error bodies.
        """
        allowed = (
            product == "jira"
            and re.fullmatch(r"/rest/api/3/(field|issue/[A-Z0-9_-]+(?:/remotelink)?)", path)
            or product == "confluence"
            and re.fullmatch(r"/wiki/(api/v2/pages/[0-9]+|rest/api/search)", path)
        )
        if not allowed:
            raise RetrievalError("Atlassian operation is not on the read allowlist")
        if self.config.cloud_id:
            base = f"https://api.atlassian.com/ex/{product}/{self.config.cloud_id}"
        else:
            base = self.config.site_url
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {self._auth[product]}",
                "User-Agent": "dev-agent/0.1 investigation",
            },
        )
        try:
            with self._opener.open(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(self.config.max_response_bytes + 1)
                if len(raw) > self.config.max_response_bytes:
                    raise RetrievalError(f"{product} response exceeded configured size limit")
                return self._clean(json.loads(raw.decode("utf-8")))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                message = "authentication/permission failure"
            elif exc.code == 404:
                message = "issue/page not found or not visible"
            elif exc.code == 429:
                message = "rate limited; no automatic retry performed"
            else:
                message = "read request failed"
            raise RetrievalError(f"{product}: {message} (HTTP {exc.code})") from None
        except OSError, ValueError, RecursionError, urllib.error.URLError:
            raise RetrievalError(f"{product}: transport or JSON response failure") from None

    def _optional(self, label: str, callback: Callable[[], Any], default: Any) -> Any:
        """Record a diagnostic and substitute the default when an optional read fails."""
        try:
            return callback()
        except RetrievalError as exc:
            self.warnings.append(f"{label}: {self.sanitize(str(exc))}")
            return default
        except TypeError, ValueError, AttributeError:
            self.warnings.append(f"{label}: invalid optional API response")
            return default

    def _issue(self, task: str, acceptance: list[str]) -> Document:
        """Fetch and validate a Jira issue and preserve its requirement fields and revision."""
        fields = [
            "summary",
            "description",
            "issuetype",
            "status",
            "updated",
            "issuelinks",
            *acceptance,
        ]
        data = self._get(
            "jira", f"/rest/api/3/issue/{jira_key(task)}", {"fields": ",".join(fields)}
        )
        if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
            raise RetrievalError("Jira issue response is incomplete")
        value = data["fields"]
        if not isinstance(value.get("summary"), str) or not value["summary"].strip():
            raise RetrievalError("Jira issue title is unavailable")
        if any(
            not isinstance(value.get(key), dict) or not isinstance(value[key].get("name"), str)
            for key in ("issuetype", "status")
        ):
            raise RetrievalError("Jira issue type/status is unavailable")
        if data.get("key") != task:
            raise RetrievalError(
                "Jira returned a different issue key; issue moves require human review"
            )
        for field in acceptance:
            if field not in value:
                self.warnings.append(f"jira:{task}: requested acceptance field {field} unavailable")
        issue = {
            "source_id": f"jira:{task}",
            "id": str(data.get("id", "")),
            "key": task,
            "url": f"{self.config.site_url}/browse/{task}",
            "title": value["summary"],
            "description": value.get("description"),
            "acceptance_criteria": {field: value.get(field) for field in acceptance},
            "issue_type": value.get("issuetype", {}).get("name"),
            "status": value.get("status", {}).get("name"),
            "revision": {"updated": value.get("updated")},
            "retrieved_at": now(),
        }
        if not issue["revision"]["updated"]:
            self.warnings.append(f"jira:{task}: revision unavailable; freshness not verified")
        links = value.get("issuelinks", [])
        if not isinstance(links, list):
            raise RetrievalError("Jira issue link response is invalid")
        issue["linked_issue_refs"] = []
        for link in links:
            if not isinstance(link, dict):
                continue
            for direction in ("inwardIssue", "outwardIssue"):
                target = link.get(direction, {})
                if isinstance(target, dict) and JIRA_KEY.fullmatch(str(target.get("key", ""))):
                    kind = link.get("type")
                    issue["linked_issue_refs"].append(
                        {
                            "key": target["key"],
                            "direction": direction,
                            "relationship": kind.get("name", "unspecified")
                            if isinstance(kind, dict)
                            else "unspecified",
                        }
                    )
        issue["content_sha256"] = content_hash(
            {k: v for k, v in issue.items() if k != "retrieved_at"}
        )
        return issue

    def _page_id(self, url: str) -> str | None:
        """Extract a supported numeric Confluence page ID from a same-site HTTPS URL."""
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or parsed.netloc != urllib.parse.urlsplit(self.config.site_url).netloc
        ):
            return None
        match = re.search(r"/wiki/(?:spaces/[^/]+/pages/|pages/)([0-9]+)(?:/|$)", parsed.path)
        if match:
            return match[1] if len(match[1]) <= 32 else None
        if parsed.path == "/wiki/pages/viewpage.action":
            value = urllib.parse.parse_qs(parsed.query).get("pageId", [""])[0]
            return value if re.fullmatch(r"[0-9]{1,32}", value) else None
        return None

    def _page(self, page_id: str) -> Document:
        """Read one Confluence ADF page and retain its source hash and revision evidence."""
        data = self._get(
            "confluence",
            f"/wiki/api/v2/pages/{page_id}",
            {"body-format": "atlas_doc_format", "include-version": "true"},
        )
        if not isinstance(data, dict) or str(data.get("id")) != page_id:
            raise RetrievalError("Confluence page response is incomplete")
        body = data.get("body", {}).get("atlas_doc_format", {}).get("value")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                raise RetrievalError("Confluence ADF body is invalid") from None
        if not isinstance(body, dict):
            raise RetrievalError("Confluence page ADF body is unavailable")
        version = data.get("version", {})
        revision = {"number": version.get("number"), "created_at": version.get("createdAt")}
        if type(revision["number"]) is not int:
            self.warnings.append(
                f"confluence:{page_id}: revision unavailable; freshness not verified"
            )
        page = {
            "source_id": f"confluence:{page_id}",
            "id": page_id,
            "title": data.get("title", ""),
            "url": f"{self.config.site_url}/wiki/pages/viewpage.action?pageId={page_id}",
            "parent_id": data.get("parentId"),
            "space_id": data.get("spaceId"),
            "format": "atlas_doc_format",
            "body": self._clean(body),
            "revision": revision,
            "retrieved_at": now(),
        }
        page["content_sha256"] = content_hash(
            {k: v for k, v in page.items() if k != "retrieved_at"}
        )
        return page

    def retrieve(self, task: str) -> Document:
        """Retrieve original Jira requirements and bounded supporting sources.

        The primary Jira issue is mandatory. Optional linked issues, pages and discovery
        failures are recorded as warnings. The snapshot keeps editable-source revision and
        sanitized content-hash evidence for later assessment.
        """
        jira_key(task)
        self.warnings = []
        self._redacted = False
        acceptance = list(self.config.acceptance_fields)
        if self.config.discover_acceptance_fields:
            fields = self._optional(
                "Acceptance-field discovery", lambda: self._get("jira", "/rest/api/3/field"), []
            )
            if not isinstance(fields, list):
                self.warnings.append("Acceptance-field discovery returned an invalid field list")
                fields = []
            acceptance += [
                f["id"]
                for f in fields
                if isinstance(f, dict)
                and re.fullmatch(r"customfield_[0-9]+", str(f.get("id", "")))
                and re.sub(r"[^a-z]", "", str(f.get("name", "")).lower()) == "acceptancecriteria"
            ]
        acceptance = list(dict.fromkeys(acceptance))
        if len(acceptance) > 32:
            raise RetrievalError(
                "More than 32 acceptance fields discovered; configure explicit fields"
            )
        issue = self._issue(task, acceptance)
        linked = []
        refs = list(
            dict.fromkeys(ref["key"] for ref in issue["linked_issue_refs"] if ref["key"] != task)
        )
        if len(refs) > self.config.max_linked_issues:
            self.warnings.append(
                "Linked issue retrieval capped; remaining references are preserved"
            )
        for key in refs[: self.config.max_linked_issues]:
            value = self._optional(
                f"Linked issue {key}", lambda key=key: self._issue(key, acceptance), None
            )
            if value:
                linked.append(value)
        remote = self._optional(
            "Jira remote links",
            lambda: self._get("jira", f"/rest/api/3/issue/{task}/remotelink"),
            [],
        )
        if not isinstance(remote, list):
            self.warnings.append("Jira remote links unavailable: invalid response")
            remote = []
        urls = discover_urls([issue, linked])
        for link in remote:
            obj = link.get("object") if isinstance(link, dict) else None
            if isinstance(obj, dict) and isinstance(obj.get("url"), str):
                urls.append(obj["url"])
            else:
                self.warnings.append("Jira remote link unavailable: invalid object")
        page_ids = list(self.config.confluence_page_ids)
        references = []
        for url in dict.fromkeys(urls):
            if not isinstance(url, str):
                continue
            page_id = self._page_id(url)
            if page_id:
                page_ids.append(page_id)
                references.append(
                    {"source_id": f"confluence:{page_id}", "discovered_by": "explicit_link"}
                )
            elif "/wiki" in url:
                # No arbitrary URL retrieval, redirects or cross-tenant credential forwarding.
                self.warnings.append(
                    "A Confluence-like link was not retrievable by same-site numeric page ID"
                )
        if self.config.search_confluence and self.config.max_confluence_pages:
            search = self._optional(
                "Confluence issue-key search",
                lambda: self._get(
                    "confluence",
                    "/wiki/rest/api/search",
                    {
                        "cql": f'type = page AND text ~ "\\"{task}\\""',
                        "limit": self.config.max_confluence_pages,
                    },
                ),
                {},
            )
            if not isinstance(search, dict):
                search = {}
                self.warnings.append("Confluence search response is invalid")
            found = search.get("results", [])
            if not isinstance(found, list):
                found = []
                self.warnings.append("Confluence search results unavailable: invalid response")
            for result in found:
                content = result.get("content") if isinstance(result, dict) else None
                value = content.get("id") if isinstance(content, dict) else None
                if isinstance(value, str) and re.fullmatch(r"[0-9]{1,32}", value):
                    page_ids.append(value)
                    references.append(
                        {"source_id": f"confluence:{value}", "discovered_by": "issue_key_search"}
                    )
            links = search.get("_links")
            if isinstance(links, dict) and links.get("next"):
                self.warnings.append(
                    "Confluence search has further pages; retrieval is deliberately bounded"
                )
        else:
            self.warnings.append("Confluence issue-key search disabled or page limit is zero")
        page_ids = list(dict.fromkeys(page_ids))
        if len(page_ids) > self.config.max_confluence_pages:
            self.warnings.append(
                "Confluence page retrieval capped; not all discovered documents were loaded"
            )
        pages = []
        for page_id in page_ids[: self.config.max_confluence_pages]:
            page = self._optional(
                f"Confluence page {page_id}", lambda page_id=page_id: self._page(page_id), None
            )
            if page:
                pages.append(page)
        requirements = requirement_units(issue)
        if self._redacted:
            self.warnings.append(
                "Known credential values redacted; stored source text is not "
                "byte-identical to upstream"
            )
        if len(requirements) == 1:
            self.warnings.append(
                "Only the title is available as a requirement; acceptance completeness is unknown"
            )
        return {
            "schema_version": 1,
            "provider": "atlassian",
            "task_id": task,
            "retrieved_at": now(),
            "authority": "Primary Jira issue; linked issues and Confluence are supporting "
            "context, conflicts require explicit reporting",
            "issue": issue,
            "requirements": requirements,
            "linked_issues": linked,
            "confluence_documents": pages,
            "document_references": references,
            "warnings": list(dict.fromkeys(self.warnings)),
            "omissions": [
                "Comments, attachments, arbitrary URLs and transitive link "
                "crawling are not retrieved",
                "Retrieval is not a cross-service transactional snapshot",
            ],
        }


def discover_urls(value: Any) -> list[str]:
    """Collect apparent HTTPS links from strings nested in retrieved data."""
    urls = []
    if isinstance(value, str):
        urls += re.findall(r'https://[^\s<>"\]})]+', value)
    elif isinstance(value, dict):
        for item in value.values():
            urls.extend(discover_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(discover_urls(item))
    return urls


def rich_text(value: Any) -> str:
    """Render a text view of an ADF value without replacing the retained original."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(rich_text(v) for v in value)
    if isinstance(value, dict):
        if value.get("type") == "hardBreak":
            return "\n"
        text = value.get("text", "")
        children = value.get("content", [])
        text += "".join(rich_text(v) for v in children)
        attrs = value.get("attrs", {})
        if value.get("type") in ("inlineCard", "blockCard"):
            text += str(attrs.get("url", ""))
        return text
    return "" if value is None else json.dumps(value, ensure_ascii=True)


def requirement_units(issue: Document) -> list[Document]:
    """Derive stable requirement IDs and text from original Jira description and criteria."""
    units = []

    def add(pointer: str, text: str) -> None:
        """Append a nonblank requirement tied to its exact source pointer."""
        if text.strip():
            units.append(
                {
                    "requirement_id": f"{issue['source_id']}:{pointer}",
                    "source_id": issue["source_id"],
                    "pointer": pointer,
                    "text": text,
                }
            )

    def walk(value: Any, pointer: str) -> None:
        """Visit supported ADF blocks and strings in source order to extract requirements."""
        if isinstance(value, dict):
            if value.get("type") in ("paragraph", "heading", "codeBlock"):
                add(pointer, rich_text(value))
            elif isinstance(value.get("content"), list) and value["content"]:
                for index, child in enumerate(value["content"]):
                    walk(child, f"{pointer}/content/{index}")
            else:
                add(pointer, rich_text(value) or json.dumps(value, ensure_ascii=True))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{pointer}/{index}")
        elif isinstance(value, str):
            for index, line in enumerate(value.splitlines()):
                add(f"{pointer}/line/{index}", line)
        elif value is not None:
            add(pointer, rich_text(value))

    add("/title", issue["title"])
    walk(issue["description"], "/description")
    for field, value in issue["acceptance_criteria"].items():
        walk(value, f"/acceptance_criteria/{field}")
    return units
