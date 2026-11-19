"""Expose the Dev Agent command-line interface and terminal output.

The parser defines operator intent and the dispatcher loads external TOML before invoking
workflows. This module translates records and expected exceptions into output and exit codes.
"""

import argparse
import json
import signal
import sys
from pathlib import Path

from dev_agent.config import default_config_path, load_config
from dev_agent.delivery.implementation import implement
from dev_agent.errors import DevAgentError
from dev_agent.records import RunRecord, Store
from dev_agent.reporting.terminal import (
    print_evidence,
    render_list,
    render_run,
    retained,
    terminal_text,
)
from dev_agent.run_admin import cleanup, inspect_run
from dev_agent.verification.runs import verify


def pr_mode_argument(command: argparse.ArgumentParser) -> None:
    """Require an explicit publication mode and preserve the legacy draft spelling."""
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--draft",
        dest="pr_mode",
        action="store_const",
        const="draft",
        help="Alias for --pr-mode draft",
    )
    mode.add_argument("--pr-mode", choices=("draft", "ready"), help="Select the created PR type")


def parser() -> argparse.ArgumentParser:
    """Build the argparse command tree, including required flags and preview options."""
    root = argparse.ArgumentParser(
        prog="dev-agent", description="Investigate Jira tasks or implement approved worktree tasks"
    )
    root.add_argument(
        "--config", type=Path, default=default_config_path(), help="External TOML config"
    )
    root.add_argument("--json", action="store_true", help="Write structured output to stdout")
    output_options = argparse.ArgumentParser(add_help=False)
    output_options.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write structured output to stdout",
    )
    commands = root.add_subparsers(dest="command", required=True)
    projects = commands.add_parser("projects", parents=[output_options]).add_subparsers(
        dest="action", required=True
    )
    projects.add_parser("list", parents=[output_options])
    task = commands.add_parser("implement", parents=[output_options])
    task.add_argument("task")
    task.add_argument("--project", required=True)
    task.add_argument("--prompt-file", type=Path, required=True)
    task.add_argument("--dry-run", action="store_true")
    task.add_argument("--verification", choices=("targeted", "full", "legacy"))
    task.add_argument(
        "--scope",
        action="append",
        help="Approved baseline file/directory; repeat to select several (no globs)",
    )
    adoption = commands.add_parser(
        "adopt",
        parents=[output_options],
        help="Qualify a retained legacy implementation without running Codex implementation",
    )
    adoption.add_argument("run_id", metavar="ORIGINAL-RUN-ID")
    adoption.add_argument("--requirements-run", metavar="INVESTIGATION-RUN-ID")
    adoption.add_argument(
        "--approve-head",
        metavar="FULL-SHA",
        help="Reviewed, already-published feature branch parent",
    )
    adoption.add_argument("--prepare", action="store_true")
    adoption.add_argument("--accept-legacy", metavar="APPROVAL-SHA256")
    investigation = commands.add_parser(
        "investigate",
        parents=[output_options],
        help="Read-only Jira/baseline investigation; never start implementation",
    )
    investigation.add_argument("task")
    investigation.add_argument("--project", required=True)
    investigation.add_argument("--dry-run", action="store_true")
    runs = commands.add_parser("runs", parents=[output_options]).add_subparsers(
        dest="action", required=True
    )
    runs.add_parser("list", parents=[output_options])
    runs.add_parser("show", parents=[output_options]).add_argument("run_id")
    commands.add_parser("inspect", parents=[output_options]).add_argument("run_id")
    commands.add_parser(
        "results", parents=[output_options], help="Print a run's exported results directory"
    ).add_argument("run_id")
    verification = commands.add_parser(
        "verify",
        parents=[output_options],
        help="Append verification against a retained worktree; never run Codex",
    )
    verification.add_argument("run_id")
    verification.add_argument("--profile", choices=("targeted", "full", "legacy"))
    verification.add_argument(
        "--prepare",
        action="store_true",
        help="Explicitly run configured preparation before verification",
    )
    publication = commands.add_parser(
        "publish", parents=[output_options], help="Preview or confirm GitHub PR publication"
    )
    publication.add_argument("run_id")
    pr_mode_argument(publication)
    approval = publication.add_mutually_exclusive_group()
    approval.add_argument("--dry-run", action="store_true")
    approval.add_argument("--confirm", action="store_true")
    publication.add_argument(
        "--file", action="append", help="Literal approved file; repeat for the complete manifest"
    )
    publication.add_argument("--title", help="Reviewed ticket-prefixed PR/commit title")
    publication.add_argument("--summary", help="Reviewed concise PR summary")
    publication.add_argument("--plan-sha256", help="Require exactly the reviewed preview identity")
    publication.add_argument("--allow-incomplete-full", action="store_true")
    publication.add_argument("--full-verification-reason")
    publication.add_argument("--acknowledge-stale-base", action="store_true")
    branch = commands.add_parser(
        "push",
        parents=[output_options],
        help="Preview or confirm a managed branch push without creating a PR",
    )
    branch.add_argument("run_id")
    approval = branch.add_mutually_exclusive_group()
    approval.add_argument("--dry-run", action="store_true")
    approval.add_argument("--confirm", action="store_true")
    branch.add_argument(
        "--file", action="append", help="Repeat for every approved implementation path"
    )
    branch.add_argument("--title", help="Reviewed commit title")
    branch.add_argument(
        "--approve-head",
        metavar="FULL-SHA",
        help="Approve the exact already-published feature branch parent",
    )
    branch.add_argument(
        "--plan-sha256", help="Reviewed push preview digest; required with --confirm"
    )
    ci = commands.add_parser(
        "ci", parents=[output_options], help="Observe checks for the recorded published commit"
    )
    ci.add_argument("run_id")
    ci.add_argument("--watch", action="store_true")
    delivery = commands.add_parser(
        "deliver",
        parents=[output_options],
        help="MCP-only ticket delivery; fail closed if intake is unavailable",
    )
    delivery.add_argument("task")
    delivery.add_argument("--project", required=True)
    pr_mode_argument(delivery)
    delivery.add_argument(
        "--confirm", action="store_true", help="Authorise commit, push and the selected PR only"
    )
    delivery.add_argument("--watch", action="store_true")
    delivery.add_argument("--dry-run", action="store_true")
    delivery.add_argument("--resume", metavar="EXECUTION-ID")
    clean = commands.add_parser(
        "cleanup",
        parents=[output_options],
        description="Remove a worktree or investigation snapshot; retain run records",
    )
    clean.add_argument("run_id")
    clean.add_argument("--yes", action="store_true", help="Explicitly confirm removal")
    clean.add_argument(
        "--force",
        action="store_true",
        help="Discard dirty worktree files or a changed investigation snapshot permanently",
    )
    return root


def dump(value: object) -> None:
    """Print indented, ASCII-escaped JSON for stable terminal inspection."""
    # ASCII escaping also prevents terminal control sequences in filenames/agent errors.
    print(json.dumps(value, indent=2, ensure_ascii=True))


def exit_code(outcome: str) -> int:
    """Map a workflow outcome to its documented command-line exit status."""
    return {
        "passed": 0,
        "succeeded": 0,
        "unverified": 4,
        "passed_with_warnings": 4,
        "agent_failed": 3,
        "verification_failed": 4,
        "environment_failed": 6,
        "interrupted": 130,
        "investigated": 0,
        "retrieval_failed": 7,
        "context_failed": 6,
        "invalid_investigation": 8,
        "security_failed": 9,
        "instructions_failed": 10,
    }.get(outcome, 5)


def main(argv: list[str] | None = None) -> int:
    """Dispatch a CLI invocation and return its process exit status.

    Parse the supplied argument list or process arguments, load operator configuration and bind
    SIGTERM for the invocation. A handler may perform the side effects authorised by its
    subcommand. Invalid input or an actionable runtime refusal returns 2 after printing a
    diagnostic.

    Returns:
        The command exit code, which can distinguish an incomplete result from a local error.
    """
    args = parser().parse_args(argv)
    cancelled = False

    def interrupted(signum: int, frame: object) -> None:
        """Allow the first cancellation to unwind, including durable evidence and child cleanup."""
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            raise KeyboardInterrupt

    signals = (signal.SIGINT, signal.SIGTERM)
    if args.command == "adopt":
        # A terminal suspension must not freeze adoption while it owns a worktree lock.
        signals += (signal.SIGTSTP,)
    previous = {sig: signal.signal(sig, interrupted) for sig in signals}
    store = None

    def show(
        record: RunRecord,
        *,
        data: object | None = None,
        preview: RunRecord | None = None,
        inspection: RunRecord | None = None,
        observation: RunRecord | None = None,
        code: int = 0,
    ) -> int:
        """Write the existing structured payload or its terminal view."""
        if args.json:
            dump(record if data is None else data)
            if code and record:
                print_evidence(store, record, sys.stderr)
        else:
            render_run(
                store,
                record,
                args.command,
                preview=preview,
                inspection=inspection,
                observation=observation,
            )
        return code

    def progress(stage: str) -> None:
        """Stream phase notices on stderr, separate from the final stdout value."""
        detail = json.dumps(stage) if args.json else terminal_text(stage.replace("_", " "))
        operation = "delivery" if args.command == "deliver" else args.command
        try:
            print(f"dev-agent {operation}: {detail}", file=sys.stderr, flush=True)
        except OSError:
            pass  # A closed diagnostic stream must not interrupt a running workflow.

    try:
        config = load_config(args.config)
        store = Store(config.runs_root)
        if args.command == "projects":
            rows = [
                {
                    "name": p.name,
                    "repository": str(p.repository),
                    "base_branch": p.base_branch,
                    "verification": [c.name for c in p.verification],
                    "verification_policy": p.verification_policy.evidence(),
                }
                for p in config.projects.values()
            ]
            if args.json:
                dump(rows)
            else:
                render_list(rows, ("name", "base_branch", "repository"))
        elif args.command == "implement":
            record = implement(
                config,
                args.project,
                args.task,
                args.prompt_file,
                dry_run=args.dry_run,
                verification=args.verification,
                scope=tuple(args.scope) if args.scope else None,
                progress=None if args.json else progress,
            )
            if args.dry_run:
                show({}, data={"dry_run": True, "plan": record}, preview=record)
                return 0
            return show(record, code=exit_code(record["outcome"]))
        elif args.command == "adopt":
            from dev_agent.delivery.adoption import adopt, summary

            record = adopt(
                config,
                args.run_id,
                requirements_run=args.requirements_run,
                prepare=args.prepare,
                accept_legacy=args.accept_legacy,
                approve_head=args.approve_head,
            )
            status = record["adoption"]["status"]
            if status == "interrupted":
                code = 130
            else:
                code = 0 if status in ("awaiting_legacy_approval", "approved") else 13
            return show(record, data=summary(record) if args.json else None, code=code)
        elif args.command == "investigate":
            from dev_agent.investigation.workflow import investigate

            record = investigate(config, args.project, args.task, dry_run=args.dry_run)
            if args.dry_run:
                return show({}, data={"dry_run": True, "plan": record}, preview=record)
            return show(record, code=exit_code(record["outcome"]))
        elif args.command == "deliver":
            from dev_agent.delivery.workflow import deliver
            from dev_agent.reporting.delivery import summary

            record = deliver(
                config,
                args.project,
                args.task,
                confirmed=args.confirm,
                watch=args.watch,
                dry_run=args.dry_run,
                resume=args.resume,
                pr_mode=args.pr_mode,
                progress=progress,
            )
            code = 0 if record.get("outcome") == "delivered" else 13
            if args.dry_run:
                return show({}, data=record, preview=record, code=code)
            return show(record, data=summary(config, record) if args.json else None, code=code)
        elif args.command == "runs":
            if args.action == "show":
                show(store.load(args.run_id))
            else:
                rows = [
                    {
                        k: r.get(k)
                        for k in (
                            "run_id",
                            "project",
                            "task",
                            "outcome",
                            "created_at",
                            "cleanup",
                        )
                    }
                    | {
                        "kind": r.get("kind", "implementation"),
                        "results_directory": r.get("results_directory"),
                        "latest_verification_outcome": (r.get("verification_attempts") or [{}])[
                            -1
                        ].get("outcome"),
                    }
                    for r in store.list()
                ]
                if args.json:
                    dump(rows)
                else:
                    render_list(rows, ("run_id", "project", "task", "kind", "outcome"))
        elif args.command == "inspect":
            info = inspect_run(config, args.run_id)
            show({} if args.json else store.load(args.run_id), data=info, inspection=info)
        elif args.command == "results":
            record = store.load(args.run_id)
            directory = record.get("results_directory")
            if not directory or not Path(directory).is_dir():
                raise DevAgentError(
                    "No exported results for this run; historical evidence remains in runs_root"
                )
            if args.json:
                dump({"results_directory": directory})
            else:
                print(terminal_text(directory))
        elif args.command == "verify":
            record = verify(config, args.run_id, args.profile, prepare_environment=args.prepare)
            attempt = record["verification_attempts"][-1]
            code = 5 if record.get("results_error") else exit_code(attempt["outcome"])
            return show(record, data=attempt, code=code)
        elif args.command == "cleanup":
            # Inspect before prompting so confirmation refers to a concrete, validated path.
            info = inspect_run(config, args.run_id)
            messages = sys.stderr if args.json else sys.stdout
            path = info.get("worktree") or info.get("snapshot_directory")
            if path:
                print(f"Cleanup target: {terminal_text(path)}", file=messages)
            print(
                "Cleanup removes the investigation snapshot; evidence is retained."
                if info.get("kind") == "investigation"
                else "Cleanup removes the worktree. Its branch and run records are retained.",
                file=messages,
            )
            if args.force:
                print(
                    "FORCE: Changed investigation snapshot contents will be permanently lost; "
                    "other investigation evidence is retained."
                    if info.get("kind") == "investigation"
                    else "FORCE: ALL uncommitted, untracked and ignored worktree files "
                    "will be permanently lost.",
                    file=messages,
                )
            confirmed = args.yes
            if not confirmed and sys.stdin.isatty():
                print(
                    f"Type {terminal_text(args.run_id)} to confirm removal: ",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
                confirmed = input() == args.run_id
            record = cleanup(config, args.run_id, confirmed=confirmed, force=args.force)
            show(record, data={"run_id": args.run_id, "cleanup": record["cleanup"]})
        elif args.command in ("push", "publish"):
            from dev_agent.delivery.publication import publish_run

            options = (
                {"branch_only": True, "approve_head": args.approve_head}
                if args.command == "push"
                else {
                    "pr_mode": args.pr_mode,
                    "summary": args.summary,
                    "allow_incomplete_full": args.allow_incomplete_full,
                    "incomplete_full_reason": args.full_verification_reason,
                    "acknowledge_stale_base": args.acknowledge_stale_base,
                }
            )
            result = publish_run(
                config,
                args.run_id,
                confirmed=args.confirm,
                dry_run=args.dry_run,
                files=tuple(args.file) if args.file else None,
                title=args.title,
                plan_sha256=args.plan_sha256,
                **options,
            )
            if "plan" in result:
                return show(
                    retained(store, args.run_id),
                    data=result,
                    preview=result["plan"],
                    code=2 if result["plan"]["blockers"] else 0,
                )
            publication = result["publication"]
            if args.command == "push":
                code = 0 if publication["status"] == "pushed" else 11
            elif publication["status"] != "published":
                code = 11
            else:
                observation = publication["ci_observations"][-1]
                code = 0 if observation["status"] == "passed" else 12
            return show(result, data=publication, code=code)
        elif args.command == "ci":
            from dev_agent.delivery.publication import ci_run

            observation = ci_run(config, args.run_id, watch=args.watch)
            return show(
                retained(store, args.run_id),
                data=observation,
                observation=observation,
                code=0 if observation["status"] == "passed" else 12,
            )
        return 0
    except (DevAgentError, OSError, ValueError, KeyError) as exc:
        print(f"dev-agent: {terminal_text(exc)}", file=sys.stderr)
        if record := retained(
            store, getattr(args, "run_id", None) or getattr(args, "resume", None)
        ):
            print_evidence(store, record, sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("dev-agent: interrupted", file=sys.stderr)
        if record := retained(
            store, getattr(args, "run_id", None) or getattr(args, "resume", None)
        ):
            print_evidence(store, record, sys.stderr)
        return 130
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
