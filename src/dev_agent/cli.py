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
from dev_agent.records import Store
from dev_agent.run_admin import cleanup, inspect_run
from dev_agent.verification.runs import verify


def parser() -> argparse.ArgumentParser:
    """Build the argparse command tree, including required flags and preview options."""
    root = argparse.ArgumentParser(
        prog="dev-agent", description="Investigate Jira tasks or implement approved worktree tasks"
    )
    root.add_argument(
        "--config", type=Path, default=default_config_path(), help="External TOML config"
    )
    commands = root.add_subparsers(dest="command", required=True)
    projects = commands.add_parser("projects").add_subparsers(dest="action", required=True)
    projects.add_parser("list")
    task = commands.add_parser("implement")
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
    investigation = commands.add_parser(
        "investigate", help="Read-only Jira/baseline investigation; never start implementation"
    )
    investigation.add_argument("task")
    investigation.add_argument("--project", required=True)
    investigation.add_argument("--dry-run", action="store_true")
    runs = commands.add_parser("runs").add_subparsers(dest="action", required=True)
    runs.add_parser("list")
    runs.add_parser("show").add_argument("run_id")
    commands.add_parser("inspect").add_argument("run_id")
    commands.add_parser("results", help="Print a run's exported results directory").add_argument(
        "run_id"
    )
    verification = commands.add_parser(
        "verify", help="Append verification against a retained worktree; never run Codex"
    )
    verification.add_argument("run_id")
    verification.add_argument("--profile", choices=("targeted", "full", "legacy"))
    verification.add_argument(
        "--prepare",
        action="store_true",
        help="Explicitly run configured preparation before verification",
    )
    publication = commands.add_parser("publish", help="Preview or confirm draft GitHub publication")
    publication.add_argument("run_id")
    publication.add_argument("--draft", action="store_true", required=True)
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
    ci = commands.add_parser("ci", help="Observe checks for the recorded published commit")
    ci.add_argument("run_id")
    ci.add_argument("--watch", action="store_true")
    delivery = commands.add_parser(
        "deliver", help="MCP-only ticket delivery; fail closed if intake is unavailable"
    )
    delivery.add_argument("task")
    delivery.add_argument("--project", required=True)
    delivery.add_argument("--draft", action="store_true", required=True)
    delivery.add_argument(
        "--confirm", action="store_true", help="Authorise commit, push and draft PR only"
    )
    delivery.add_argument("--watch", action="store_true")
    delivery.add_argument("--dry-run", action="store_true")
    delivery.add_argument("--resume", metavar="EXECUTION-ID")
    clean = commands.add_parser(
        "cleanup", description="Remove a worktree or investigation snapshot; retain run records"
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


def interrupted(signum: int, frame: object) -> None:
    """Translate SIGTERM into the same interruption path as Ctrl-C."""
    raise KeyboardInterrupt


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
    # SIGTERM, like Ctrl-C, gives the orchestrator a chance to stop children and save its record.
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        config = load_config(args.config)
        store = Store(config.runs_root)
        if args.command == "projects":
            dump(
                [
                    {
                        "name": p.name,
                        "repository": str(p.repository),
                        "base_branch": p.base_branch,
                        "verification": [c.name for c in p.verification],
                    }
                    for p in config.projects.values()
                ]
            )
        elif args.command == "implement":
            record = implement(
                config,
                args.project,
                args.task,
                args.prompt_file,
                dry_run=args.dry_run,
                verification=args.verification,
                scope=tuple(args.scope) if args.scope else None,
            )
            if args.dry_run:
                dump({"dry_run": True, "plan": record})
                return 0
            dump(record)
            print(f"Worktree retained: {record['worktree']}")
            print(f"Run report/logs: {store.directory(record['run_id'])}")
            print(f"Results: {record.get('results_directory', 'not exported')}")
            return exit_code(record["outcome"])
        elif args.command == "investigate":
            from dev_agent.investigation.workflow import investigate

            record = investigate(config, args.project, args.task, dry_run=args.dry_run)
            dump({"dry_run": True, "plan": record} if args.dry_run else record)
            if not args.dry_run:
                destination = record.get("results_directory") or (
                    "export unavailable; inspect canonical record"
                )
                print(f"Investigation results: {destination}")
            return 0 if args.dry_run else exit_code(record["outcome"])
        elif args.command == "deliver":
            from dev_agent.delivery.workflow import deliver
            from dev_agent.reporting.delivery import summary

            def progress(stage: str) -> None:
                """Stream a delivery phase notice to stderr without mixing it into JSON output."""
                print("dev-agent delivery: " + json.dumps(stage), file=sys.stderr, flush=True)

            record = deliver(
                config,
                args.project,
                args.task,
                confirmed=args.confirm,
                watch=args.watch,
                dry_run=args.dry_run,
                resume=args.resume,
                progress=progress,
            )
            dump(record if args.dry_run else summary(config, record))
            return 0 if record.get("outcome") == "delivered" else 13
        elif args.command == "runs":
            if args.action == "show":
                dump(store.load(args.run_id))
            else:
                dump(
                    [
                        {
                            k: r[k]
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
                )
        elif args.command == "inspect":
            dump(inspect_run(config, args.run_id))
        elif args.command == "results":
            record = store.load(args.run_id)
            directory = record.get("results_directory")
            if not directory or not Path(directory).is_dir():
                raise DevAgentError(
                    "No exported results for this run; historical evidence remains in runs_root"
                )
            print(directory)
        elif args.command == "verify":
            record = verify(config, args.run_id, args.profile, prepare_environment=args.prepare)
            dump(record["verification_attempts"][-1])
            print(f"Original outcome: {record['outcome']}; original agent evidence retained")
            print(f"Results: {record.get('results_directory', 'not exported')}")
            return (
                5
                if record.get("results_error")
                else exit_code(record["verification_attempts"][-1]["outcome"])
            )
        elif args.command == "cleanup":
            # Inspect before prompting so confirmation refers to a concrete, validated path.
            info = inspect_run(config, args.run_id)
            dump(info)
            print(
                "Cleanup removes the investigation snapshot; evidence is retained."
                if info.get("kind") == "investigation"
                else "Cleanup removes the worktree. Its branch and run records are retained."
            )
            if args.force:
                print(
                    "FORCE: Changed investigation snapshot contents will be permanently lost; "
                    "other investigation evidence is retained."
                    if info.get("kind") == "investigation"
                    else "FORCE: ALL uncommitted, untracked and ignored worktree files "
                    "will be permanently lost."
                )
            confirmed = args.yes
            if not confirmed and sys.stdin.isatty():
                confirmed = input(f"Type {args.run_id} to confirm removal: ") == args.run_id
            record = cleanup(config, args.run_id, confirmed=confirmed, force=args.force)
            dump({"run_id": args.run_id, "cleanup": record["cleanup"]})
        elif args.command == "publish":
            from dev_agent.delivery.publication import publish_run

            result = publish_run(
                config,
                args.run_id,
                confirmed=args.confirm,
                dry_run=args.dry_run,
                files=tuple(args.file) if args.file else None,
                title=args.title,
                summary=args.summary,
                plan_sha256=args.plan_sha256,
                allow_incomplete_full=args.allow_incomplete_full,
                incomplete_full_reason=args.full_verification_reason,
                acknowledge_stale_base=args.acknowledge_stale_base,
            )
            dump(result.get("publication", result))
            if "plan" in result:
                return 2 if result["plan"]["blockers"] else 0
            if result["publication"]["status"] != "published":
                return 11
            observation = result["publication"]["ci_observations"][-1]
            return 0 if observation["status"] == "passed" else 12
        elif args.command == "ci":
            from dev_agent.delivery.publication import ci_run

            observation = ci_run(config, args.run_id, watch=args.watch)
            dump(observation)
            return 0 if observation["status"] == "passed" else 12
        return 0
    except (DevAgentError, OSError, ValueError, KeyError) as exc:
        print(f"dev-agent: {str(exc)!r}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("dev-agent: interrupted", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
