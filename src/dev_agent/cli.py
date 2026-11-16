"""The public CLI. JSON is used for plans, reports and inspection."""

import argparse
import json
import signal
import sys
from pathlib import Path

from dev_agent.config import default_config_path, load_config
from dev_agent.errors import DevAgentError
from dev_agent.orchestrator import cleanup, implement, inspect_run
from dev_agent.records import Store


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="dev-agent", description="Implement approved tasks in retained Git worktrees"
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
    runs = commands.add_parser("runs").add_subparsers(dest="action", required=True)
    runs.add_parser("list")
    runs.add_parser("show").add_argument("run_id")
    commands.add_parser("inspect").add_argument("run_id")
    clean = commands.add_parser(
        "cleanup", description="Remove a worktree; retain branch and run records"
    )
    clean.add_argument("run_id")
    clean.add_argument("--yes", action="store_true", help="Explicitly confirm removal")
    clean.add_argument(
        "--force",
        action="store_true",
        help="Permanently discard uncommitted, untracked and ignored files",
    )
    return root


def dump(value: object) -> None:
    # ASCII escaping also prevents terminal control sequences in filenames/agent errors.
    print(json.dumps(value, indent=2, ensure_ascii=True))


def interrupted(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
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
                config, args.project, args.task, args.prompt_file, dry_run=args.dry_run
            )
            if args.dry_run:
                dump({"dry_run": True, "plan": record})
                return 0
            dump(record)
            print(f"Worktree retained: {record['worktree']}")
            print(f"Run report/logs: {store.directory(record['run_id'])}")
            return {
                "succeeded": 0,
                "unverified": 4,
                "agent_failed": 3,
                "verification_failed": 4,
                "interrupted": 130,
            }.get(record["outcome"], 5)
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
                        for r in store.list()
                    ]
                )
        elif args.command == "inspect":
            dump(inspect_run(config, args.run_id))
        elif args.command == "cleanup":
            # Inspect before prompting so confirmation refers to a concrete, validated path.
            info = inspect_run(config, args.run_id)
            dump(info)
            print("Cleanup removes the worktree. Its branch and run records are retained.")
            if args.force:
                print(
                    "FORCE: ALL uncommitted, untracked and ignored worktree files "
                    "will be permanently lost."
                )
            confirmed = args.yes
            if not confirmed and sys.stdin.isatty():
                confirmed = input(f"Type {args.run_id} to confirm removal: ") == args.run_id
            record = cleanup(config, args.run_id, confirmed=confirmed, force=args.force)
            dump({"run_id": args.run_id, "cleanup": record["cleanup"]})
        return 0
    except (DevAgentError, OSError, ValueError, KeyError) as exc:
        print(f"dev-agent: {str(exc)!r}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("dev-agent: interrupted", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
