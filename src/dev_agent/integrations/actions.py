"""Qualify GitHub Actions workflows and collect required jobs for one published PR head.

Static qualification accepts a bounded subset of workflow conditions. Live evidence comes
from GitHub's workflow-run and job endpoints, scoped to the approved workflow and PR.
"""

import re
from urllib.parse import urlencode

import yaml

from dev_agent.integrations.github import GitHub, GitHubError
from dev_agent.records import RunRecord, now


class WorkflowLoader(yaml.BaseLoader):
    """Preserve the literal 'on' key and reject ambiguous mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        """Construct a mapping without accepting duplicate or compound keys."""
        mapping = super().construct_mapping(node, deep)
        if len(mapping) != len(node.value):
            raise ValueError("Duplicate workflow keys")
        return mapping


def allows_pr(condition: str, mode: str) -> bool:
    """Recognise unconditional jobs and simple draft predicates in a PR event."""
    expression = condition.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    # On a pull_request event the push arm is false.
    expression = re.sub(r"""^github\.event_name\s*==\s*['"]push['"]\s*\|\|\s*""", "", expression)
    if expression == "true":
        return True
    if re.fullmatch(r"!\s*github\.event\.pull_request\.draft", expression):
        return mode == "ready"
    match = re.fullmatch(r"github\.event\.pull_request\.draft\s*==\s*(true|false)", expression)
    return match is not None and (match[1] == "true") == (mode == "draft")


def workflow_problems(text: bytes, base: str, pr_mode: str, expected: list[str]) -> list[str]:
    """Check trigger and job eligibility without executing repository workflow content.

    Accept an unfiltered pull_request trigger or an exact base allowlist and require
    opened and synchronize when activity types are explicit. Reject ambiguous job
    identity and arbitrary job/dependency conditions that could prevent selected jobs.
    """
    if len(text) > 1024 * 1024:
        return ["workflow exceeds the safe size limit"]
    try:
        depth = 0
        for index, event in enumerate(yaml.parse(text)):
            if index > 30000 or getattr(event, "anchor", None) or getattr(event, "tag", None):
                raise ValueError("Unsupported or oversized YAML")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise ValueError("YAML nesting exceeds safe limits")
            if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        document = yaml.load(text, Loader=WorkflowLoader)
        if not isinstance(document, dict):
            raise ValueError("Expected workflow mapping")
    except yaml.YAMLError, ValueError:
        return ["workflow YAML cannot be inspected safely"]

    problems = []
    trigger = document.get("on")
    if trigger == "pull_request" or isinstance(trigger, list) and "pull_request" in trigger:
        options = {}
    elif isinstance(trigger, dict) and "pull_request" in trigger:
        options = trigger["pull_request"]
        if options in ("", "null", "~"):
            options = {}
    else:
        options = None
    if not isinstance(options, dict) or options.keys() - {"branches", "types"}:
        problems.append("requires a pull_request trigger without path or exclusion filters")
    else:
        branches = options.get("branches")
        if branches is not None and (
            not isinstance(branches, list)
            or base not in branches
            or any(
                not isinstance(name, str) or re.search(r"[!*?+\[\]\\]", name) for name in branches
            )
        ):
            problems.append(f"pull_request branches must explicitly include {base!r}")
        types = options.get("types")
        if types is not None and (
            not isinstance(types, list) or "opened" not in types or "synchronize" not in types
        ):
            problems.append("pull_request types must include opened and synchronize")

    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return [*problems, "workflow has no inspectable jobs"]

    checked: dict[str, bool] = {}

    def job_available(key: str, visiting: frozenset[str]) -> bool:
        """Check a literal job and its prerequisite jobs under the chosen PR mode."""
        if key in checked:
            return checked[key]
        job = jobs.get(key)
        if (
            key in visiting
            or len(visiting) >= 32
            or not isinstance(job, dict)
            or "uses" in job
            or "strategy" in job
            or "continue-on-error" in job
        ):
            return False
        condition = job.get("if", "true")
        if not isinstance(condition, str) or not allows_pr(condition, pr_mode):
            return False
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            return False
        # Conditional steps may skip the actual checks while the enclosing job reports success.
        if any(
            not isinstance(step, dict)
            or "continue-on-error" in step
            or not isinstance(step.get("if", "true"), str)
            or not allows_pr(step.get("if", "true"), pr_mode)
            for step in steps
        ):
            return False
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        checked[key] = isinstance(needs, list) and all(
            isinstance(dep, str) and job_available(dep, visiting | {key}) for dep in needs
        )
        return checked[key]

    for name in expected:
        matches = [
            key
            for key, job in jobs.items()
            if isinstance(job, dict) and job.get("name", key) == name
        ]
        if len(matches) != 1:
            problems.append(f"required job {name!r} must have one literal name")
        elif not job_available(matches[0], frozenset()):
            problems.append(f"required job {name!r} cannot be verified to run on a {pr_mode} PR")
    return problems


def bounded_items(data: object, key: str) -> list[RunRecord]:
    """Reject malformed or incomplete GitHub responses before considering successes."""
    if (
        not isinstance(data, dict)
        or not isinstance(items := data.get(key), list)
        or data.get("total_count") != len(items)
        or len(items) > 100
        or any(not isinstance(item, dict) for item in items)
    ):
        raise GitHubError("Required Actions evidence is invalid or exceeds the safe limit")
    return items


def required_ci_observation(
    client: GitHub,
    publication: RunRecord,
) -> RunRecord:
    """Measure required jobs for the latest matching runs of the approved PR workflows.

    Job names are scoped by workflow ID, run ID and latest attempt. An identically named
    commit status, another PR's run or an earlier attempt cannot certify the head.
    """
    commit = publication["commit_sha"]
    pr = publication["pr_number"]
    plan = publication["approved_plan"]
    observation = {
        "observed_at": now(),
        "commit_sha": commit,
        "verification_policy": plan["verification_policy"],
        "pr_mode": plan["pr_mode"],
        "checks": [],
        "status": "unable_to_retrieve",
    }
    try:
        pull = client.pull(pr)
        observation["pr_head_sha"] = pull["head"]["sha"]
        if pull["head"]["sha"] != commit:
            return observation | {"status": "commit_mismatch", "error": "PR head changed"}
        if (
            pull["head"]["ref"] != plan["branch"]
            or pull["head"]["repo"]["full_name"].lower() != client.repository.lower()
            or pull["base"]["ref"] != plan["base_branch"]
            or pull["draft"] is not (plan["pr_mode"] == "draft")
        ):
            return observation | {"status": "pr_mismatch", "error": "PR branch or mode changed"}
        observation["pr_base_sha"] = pull["base"]["sha"]
        if pull["base"]["sha"] != plan["remote_base_commit"]:
            return observation | {
                "status": "base_mismatch",
                "error": "PR base advanced beyond the approved workflow; preview again",
            }
        workflows = plan["required_workflows"]
        expected = plan["verification_policy"]["required_checks"]
        if not expected or set(expected) != set(workflows):
            raise GitHubError("Approved required workflow evidence is missing")
        query = urlencode({"event": "pull_request", "head_sha": commit, "per_page": 100})
        for path, names in expected.items():
            workflow_id = workflows[path]["id"]
            runs = bounded_items(
                client.api(f"actions/workflows/{workflow_id}/runs?{query}"), "workflow_runs"
            )
            matches = []
            for run in runs:
                if (
                    run["head_sha"] != commit
                    or run["event"] != "pull_request"
                    or run["workflow_id"] != workflow_id
                ):
                    raise GitHubError("Actions run identity differs from the request")
                if run["head_branch"] == plan["branch"] and any(
                    ref["number"] == pr
                    and ref["base"]["ref"] == plan["base_branch"]
                    and ref["base"]["sha"] == plan["remote_base_commit"]
                    for ref in run["pull_requests"]
                ):
                    matches.append(run)
            run = max(matches, key=lambda item: (item["run_number"], item["id"]), default=None)
            jobs = []
            if run:
                if any(type(run[key]) is not int or run[key] < 1 for key in ("id", "run_attempt")):
                    raise GitHubError("Invalid Actions run or attempt identity")
                jobs = bounded_items(
                    client.api(
                        f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
                    ),
                    "jobs",
                )
                if any(job["run_id"] != run["id"] or job["head_sha"] != commit for job in jobs):
                    raise GitHubError("Actions job identity differs from the requested run")
            for name in names:
                candidates = [job for job in jobs if job["name"] == name]
                if len(candidates) > 1:
                    raise GitHubError("Required Actions job name is ambiguous")
                job = candidates[0] if candidates else {}
                conclusion = job.get("conclusion")
                if not job:
                    status = "pending" if run and run["status"] != "completed" else "missing"
                elif job["status"] != "completed":
                    status = "pending"
                else:
                    status = {
                        "success": "passed",
                        "neutral": "skipped",
                        "skipped": "skipped",
                        "cancelled": "cancelled",
                        "timed_out": "timed_out",
                    }.get(conclusion, "failed")
                observation["checks"].append(
                    {
                        "workflow": path,
                        "workflow_id": workflow_id,
                        "run_id": run and run["id"],
                        "run_attempt": run and run["run_attempt"],
                        "name": name,
                        "status": status,
                        "conclusion": conclusion,
                        "url": job.get("html_url") or (run and run.get("html_url")),
                        "started_at": job.get("started_at"),
                        "completed_at": job.get("completed_at"),
                    }
                )
        states = {check["status"] for check in observation["checks"]}
        observation["status"] = next(
            (
                s
                for s in ("failed", "timed_out", "cancelled", "skipped", "pending", "missing")
                if s in states
            ),
            "passed",
        )
        if observation["status"] == "missing":
            observation["note"] = (
                "Required Actions jobs are missing for this PR head; inspect workflow "
                "triggers, PR mode and GitHub Actions permissions"
            )
    except GitHubError, KeyError, TypeError, ValueError:
        observation.update(
            status="unable_to_retrieve", error="Cannot establish required Actions evidence"
        )
    return observation
