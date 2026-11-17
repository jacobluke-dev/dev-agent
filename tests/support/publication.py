"""Temporary publication setup with fake GitHub CLI responses and intercepted Git pushes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dev_agent.config import Config, load_config
from dev_agent.delivery.implementation import implement
from dev_agent.records import RunRecord
from dev_agent.repository.git import git
from dev_agent.verification.runs import verify

type Publishing = tuple[Config, RunRecord, Path, Path]


@pytest.fixture
def publishing(
    config_file: Path,
    fake_codex: Path,
    prompt: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Publishing:
    """Create a verified temporary candidate and record fake GitHub state for publication tests."""
    config_file.write_text(
        config_file.read_text()
        + """
[[projects.example.verification_profiles.targeted]]
name = "targeted"
argv = ["/usr/bin/true"]
[[projects.example.verification_profiles.full]]
name = "full"
argv = ["/usr/bin/true"]
[projects.example.github]
repository = "example/project"
allowed_paths = ["hello.py", "new file.txt", "src", ".env", "tasks", "results"]
ci_timeout_seconds = 1
poll_interval_seconds = 1
"""
    )
    config = load_config(config_file)
    record = implement(config, "example", "TASK-42", prompt)
    record = verify(config, record["run_id"], "full")
    root = Path(record["worktree"])
    git(root, "remote", "add", "origin", "https://github.com/example/project.git")
    fake_home = tmp_path / "github-auth"
    fake_home.mkdir()
    state_file = fake_home / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "base": record["baseline_commit"],
                "head": None,
                "pull": None,
                "checks": "passed",
                "calls": [],
                "workflows": False,
            }
        )
    )
    executable = fake_codex.parent / "gh"
    executable.write_text(f"""#!{sys.executable}
import base64,json,os,pathlib,sys
p=pathlib.Path(os.environ['GH_CONFIG_DIR'])/'state.json'
s=json.loads(p.read_text());a=sys.argv[1:];s['calls'].append(a)
def finish(value=None,code=0):
 p.write_text(json.dumps(s))
 if value is not None:print(json.dumps(value))
 sys.exit(code)
if a[:2]==['auth','status']:
 if s.get('unauthenticated'):
  print('token=DO_NOT_EXPORT',file=sys.stderr);finish(code=1)
 finish()
if a[:2]==['pr','create']:
 if s.get('create_failure'):finish(code=1)
 body=pathlib.Path(a[a.index('--body-file')+1]).read_text()
 s['created_body']=body
 s['pull']={{'number':7,'html_url':'https://github.com/example/project/pull/7','draft':True,'state':'open','head':{{'sha':s['head'],'repo':{{'full_name':'example/project'}}}},'base':{{'ref':'main'}}}}
 finish('https://github.com/example/project/pull/7')
if a[0]!='api':finish(code=1)
e=a[-1]
if e=='user':finish({{'login':'example-user'}})
if e=='repos/example/project/':
 finish({{'full_name':'example/project',
         'permissions':{{'push':not s.get('no_permission')}}}})
if '/git/ref/heads/main' in e:finish({{'object':{{'sha':s['base']}}}})
if '/git/ref/heads/' in e:
 if s['head']:finish({{'object':{{'sha':s['head']}}}})
 print('HTTP 404',file=sys.stderr);finish(code=1)
if '/pulls?' in e:finish([s['pull']] if s['pull'] else [])
if '/pulls/7' in e:finish(s['pull'])
if '/contents/.github/workflows?' in e:
 if not s.get('workflows'):
  print('HTTP 404',file=sys.stderr);finish(code=1)
 finish([{{'path':'.github/workflows/ci.yml'}}])
if '/contents/.github/workflows/ci.yml?' in e:
 raw=b'on: [pull_request, push, workflow_dispatch]\\njobs:\\n  test:\\n'
 raw+=b'    if: github.event.pull_request.draft == false\\n'
 finish({{'content':base64.b64encode(raw).decode()}})
if '/check-runs?' in e:
 if s.get('checks_error'):finish(code=1)
 state=s.get('checks')
 if state=='absent':finish({{'check_runs':[]}})
 finish({{'check_runs':[{{'name':'CI','status':'in_progress' if state=='pending' else 'completed','conclusion':{{'passed':'success','failed':'failure','cancelled':'cancelled','skipped':'skipped'}}.get(state),'html_url':'https://github.com/example/project/actions/runs/1','started_at':'2026-01-01T00:00:00Z','completed_at':None}}]}})
if '/statuses?' in e:finish([])
finish(code=1)
""")
    executable.chmod(0o755)
    monkeypatch.setenv("GH_CONFIG_DIR", str(fake_home))
    original_run = subprocess.run

    def no_real_push(
        argv: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        """Intercept task-branch pushes and update synthetic remote state."""
        if argv[0] == "git" and "push" in argv:
            assert kwargs["cwd"] == root
            assert "--no-force" in argv and "--force" not in argv
            assert argv[-2] == "https://github.com/example/project.git"
            assert argv[-1].endswith(":refs/heads/" + record["branch"])
            s = json.loads(state_file.read_text())
            s["pushes"] = s.get("pushes", 0) + 1
            if s.get("push_failure"):
                state_file.write_text(json.dumps(s))
                return subprocess.CompletedProcess(argv, 1, b"", b"credential=DO_NOT_EXPORT")
            s["head"] = argv[-1].split(":")[0]
            state_file.write_text(json.dumps(s))
            return subprocess.CompletedProcess(argv, 0, b"ok", b"")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_real_push)
    return config, record, root, state_file


def state_change(path: Path, **changes: object) -> None:
    """Merge test-specific changes into the fake GitHub state file."""
    data = json.loads(path.read_text()) | changes
    path.write_text(json.dumps(data))
