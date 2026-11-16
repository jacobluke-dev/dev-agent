import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dev_agent.config import Config, load_config
from dev_agent.process import environment


def git_command(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repo),
            *args,
        ],
        env=environment() | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repository = tmp_path / "source repo"
    repository.mkdir()
    git_command(repository, "init", "-b", "main")
    git_command(repository, "config", "user.name", "Test User")
    git_command(repository, "config", "user.email", "test@example.invalid")
    (repository / "hello.py").write_text("VALUE = 1\n")
    (repository / ".gitignore").write_text("ignored/\n")
    (repository / "AGENTS.md").write_text("Use the existing package style.\n")
    git_command(repository, "add", ".")
    git_command(repository, "commit", "-m", "baseline")
    return repository


@pytest.fixture
def config_file(tmp_path: Path, repo: Path) -> Path:
    filename = tmp_path / "operator.toml"
    filename.write_text(f"""runs_root = {json.dumps(str(tmp_path / "records"))}
[projects.example]
repository = {json.dumps(str(repo))}
base_branch = "main"
worktree_root = {json.dumps(str(tmp_path / "trees"))}
[[projects.example.verification]]
name = "check"
argv = [{json.dumps(sys.executable)}, "-c", "print('verified')"]
timeout_seconds = 10
[projects.example.agent]
model = "fake-model"
reasoning_effort = "medium"
timeout_seconds = 10
""")
    return filename


@pytest.fixture
def config(config_file: Path) -> Config:
    return load_config(config_file)


@pytest.fixture
def prompt(tmp_path: Path) -> Path:
    filename = tmp_path / "task.md"
    filename.write_text("# Approved task\nChange VALUE to 2.\n")
    return filename


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local fake CLI: real pipes/files/exit codes; no provider or authentication."""
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "codex"
    executable.write_text(f"""#!{sys.executable}
import json
import pathlib
import sys
args = sys.argv[1:]
if '--help' in args:
    print('--json --output-last-message --sandbox --ephemeral --ignore-user-config --ignore-rules')
    sys.exit(0)
if '--version' in args:
    print('codex-cli fake-test')
    sys.exit(0)
prompt = sys.stdin.read()
assert 'Execution constraints' in prompt
assert 'Use the existing package style.' in prompt
assert args[0] == 'exec'
assert args[args.index('--sandbox') + 1] == 'workspace-write'
pathlib.Path('hello.py').write_text('VALUE = 2\\n')
pathlib.Path('new file.txt').write_text('new file\\n')
final = pathlib.Path(args[args.index('--output-last-message') + 1])
final.write_text('Implemented VALUE change.\\n')
print(json.dumps({{'type': 'turn.completed', 'usage': {{}}}}))
print('fake stderr', file=sys.stderr)
""")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ.get('PATH', '')}")
    return executable
