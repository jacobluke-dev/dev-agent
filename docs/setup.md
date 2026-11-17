# Setting up and operating dev-agent

Start with the [Quick Start](quickstart.md). For internal responsibilities and
workflow diagrams, see the [architecture guide](architecture.md).

See the [project overview](../README.md) or jump to the
[first execution walkthrough](#first-execution).

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Project configuration](#project-configuration)
- [Repository preparation](#repository-preparation)
- [Repository instructions and architectural context](#repository-instructions-and-architectural-context)
- [Jira investigation](#jira-investigation)
- [Environment preparation](#environment-preparation)
- [Verification](#verification)
- [Execution results](#execution-results)
- [Ticket delivery](#ticket-delivery)
- [Draft publication and CI](#draft-publication-and-ci)
- [Security](#security)
- [First execution](#first-execution)
- [Troubleshooting](#troubleshooting)
- [Current limitations](#current-limitations)

## Overview

Install dev-agent separately from the software repositories it manages. Its Python
virtual environment runs the orchestrator; it is not the managed project's runtime
or test environment.

| Component | Responsibility |
| --- | --- |
| dev-agent | Retrieve Jira requirements and investigate a baseline, or implement an approved Markdown task in a retained worktree and run checks. |
| Managed repository | Supply a local baseline branch, source code, development conventions and project tooling. |
| Git worktree | Give each execution a separate working directory and task branch at an exact baseline commit. |
| Codex CLI | Investigate a sealed baseline read-only, or implement in the task worktree, using existing local authentication. |
| External project configuration | Select repositories, trusted commands, prerequisites, profiles and storage locations. |
| Canonical run storage | Hold authoritative execution metadata, original agent evidence and verification history. |
| Exported results | Present a Markdown summary, JSON projection and logs for human inspection. |

An implementation execution uses this sequence: validate configuration and inputs; check the
Codex CLI interface; reserve storage and create a worktree; prepare its environment;
validate declared prerequisites; execute Codex; run the selected verification
profile; inspect changes and export results. The worktree remains available even
when setup, the agent or verification fails, provided creation succeeded.

An investigation retrieves requirements through GET-only Atlassian REST APIs,
preserves provenance, supplies explicit architectural context, and runs a separate
read-only Codex assessment over immutable baseline Git blobs. It creates no Git
branch/worktree, runs no setup/tests, and never starts implementation automatically.

Implementation and investigation do not commit, push or create PRs. The separate
human-confirmed publication command can commit approved source and create a draft
GitHub PR. Merging is always manual; dev-agent does not deploy or publish releases.
An agent's completion and passing command results do not constitute human approval.

## Prerequisites

| Requirement | Mandatory? | Notes |
| --- | --- | --- |
| Ubuntu/Linux | Current supported target | The implementation uses POSIX process groups and `fcntl` advisory locks. Windows is unsupported; macOS has not been qualified. |
| Python 3.14 | Yes | `.python-version` selects 3.14; package metadata requires `>=3.14`. Older Python versions are unsupported. Managed repositories can require their own Python version. |
| uv | Yes for the documented installation/development workflow | Manages the orchestrator environment and locked dependencies. |
| Git | Yes | Must be on PATH and support worktrees, porcelain/NUL output and the Git options used by this version. The source repository and shared Git metadata must be writable for worktree creation. |
| Codex CLI | Yes for implementation and investigation | Must be on PATH and support the required non-interactive and safety flags below. Independent verification does not require Codex. |
| Existing Codex authentication | Yes for real agent execution | Authenticate yourself with `codex login`. Availability of the configured model depends on your account. |
| Host connectivity to the model service | Yes for hosted Codex execution | Disabling agent shell networking does not disable Codex's model connection. |
| Bubblewrap (`bwrap`) | Required for investigation and independent review | Linux mount/PID namespaces must be enabled; there is no unrestricted fallback. Ubuntu installation: `sudo apt install bubblewrap`. |
| Atlassian API credentials | Required for investigation only | Separate local API tokens with issue/page read access; existing ChatGPT MCP authentication is not reused. |
| Project-specific runtimes and services | As configured | Node/npm, database tools, Docker, compilers and test fixtures are optional to dev-agent itself, but may be required by your managed project. |

Codex preflight runs `codex exec --help` and `codex --version` with a 15-second
limit per process. It checks support for `--json`, `--output-last-message`,
`--sandbox`, `--ephemeral`, `--ignore-user-config` and `--ignore-rules`. The adapter
identifies 0.169.0 as its tested interface; this is not a promise that every newer
release is compatible. Preflight checks installation/interface, not login or
successful model access. Dry-run skips Codex preflight entirely. Investigation
also requires `--skip-git-repo-check` and `--output-schema`, a native Codex
executable usable in the minimal namespace, and an existing `auth.json` in
`$CODEX_HOME` or `~/.codex`. Keyring-only authentication and npm wrapper scripts
are not supported by the investigation namespace. A `codex --version` namespace
probe must pass before an agent session starts.

The Python runtime has no third-party application dependencies. pytest and Ruff
are development dependencies; Hatchling is the build backend. Consult the official
[uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
and [Codex CLI installation instructions](https://developers.openai.com/codex/cli/)
if those executables are not installed.

## Installation

Obtain a reviewed checkout of this repository. Substitute its path below; it must
be separate from the repositories you intend to manage.

```bash
cd /path/to/dev-agent
uv python install 3.14
uv sync --locked
uv run python --version
uv run dev-agent --help
uv run dev-agent implement --help
uv run dev-agent verify --help
```

`uv sync --locked` uses the existing lockfile and refuses a resolution that would
change it. It can download Python/dependencies when they are not cached. The
lockfile does not pin your OS, Git, uv, Codex, managed-project dependencies or all
build-environment inputs. Use a reviewed source revision for reproducible setup.
Do not select another project's active virtual environment with `--active`.

From the source checkout, use `uv run dev-agent ...`. To expose an editable CLI
from other directories, optionally install a separate uv tool environment:

```bash
uv tool install --python 3.14 --editable .
export PATH="$(uv tool dir --bin):$PATH"
dev-agent --help
```

Keep the source checkout available for an editable installation. For a non-editable
or wheel installation, explicitly configure `results_root`: the default is computed
from the installed Python package's location, not discovered from an arbitrary
checkout. The CLI does not search your working directory for a dev-agent repository.

Check Git and Codex, then authenticate outside an agent run:

```bash
git --version
codex --version
codex exec --help
codex login
```

Use the same user and, if explicitly set, `CODEX_HOME` when authenticating and
running dev-agent. dev-agent neither implements login nor copies authentication
files. Do not put API keys or tokens into its project configuration.

For development of dev-agent itself, the existing checks are:

```bash
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

The tests use temporary Git repositories and fake/mock Codex processes; they do
not need real Codex authentication, an external account, PostgreSQL or network
access. Initial dependency installation/build requirements may still need downloads.

## Project configuration

The default configuration is external:

```text
${XDG_CONFIG_HOME:-~/.config}/dev-agent/config.toml
```

The usual location is `~/.config/dev-agent/config.toml`. There is no automatic
repository-local configuration discovery. To select a different file, place the
global flag **before** the subcommand:

```bash
uv run dev-agent --config /path/to/config.toml projects list
```

All commands that read runs also load configuration. Keep the appropriate project
and original storage settings available for old runs. `--help` works without a
configuration file. `projects list` validates configuration and lists project
names, repository paths, branches and legacy check names; it does not validate
repository availability, authentication or setup. Its check list is not a listing
of all profile commands.

### Complete synthetic example

This example assumes an existing uv-managed Python service with a committed
`uv.lock`, `pyproject.toml`, `src/` and `tests/unit/`, plus pytest and Ruff in its
project dependencies. Adapt the commands to the actual repository; the example
does not create a service or install its tooling into the dev-agent environment.
The separate [example configuration](../examples/config.toml) also demonstrates
multiple projects.

```toml
runs_root = "~/.local/state/dev-agent/runs"
results_root = "~/dev-agent-results"

[projects.example]
repository = "~/src/example-service"
base_branch = "main"
worktree_root = "~/.local/state/dev-agent/worktrees/example"
inherit_env = []

[projects.example.env]
PYTHONPATH = "{worktree}/src"

[projects.example.agent]
# model = "YOUR_SUPPORTED_CODEX_MODEL"
reasoning_effort = "medium"
timeout_seconds = 1800

[[projects.example.preparation]]
name = "python-environment"
argv = ["uv", "sync", "--locked"]
cwd = "."
timeout_seconds = 600
required = true
inherit_env = []
required_env = []
required_paths = ["pyproject.toml", "uv.lock"]

[projects.example.preparation.env]
UV_NO_PROGRESS = "1"

[projects.example.prerequisites]
env = []
paths = ["pyproject.toml", ".venv/bin/python"]
executables = ["uv"]

[[projects.example.verification_profiles.targeted]]
name = "unit-tests"
argv = ["uv", "run", "--locked", "pytest", "tests/unit"]
cwd = "."
timeout_seconds = 300

[[projects.example.verification_profiles.full]]
name = "regression"
argv = ["uv", "run", "--locked", "pytest"]
timeout_seconds = 900

[[projects.example.verification_profiles.full]]
name = "lint"
argv = ["uv", "run", "--locked", "ruff", "check", "."]
timeout_seconds = 120

[[projects.example.verification]]
name = "legacy-tests"
argv = ["uv", "run", "--locked", "pytest"]
```

Only the supported tables/properties below are accepted. Unknown settings fail
validation; Python dataclass/internal record fields are not TOML options.

### Storage and project properties

| Property | Required/default | Meaning |
| --- | --- | --- |
| `runs_root` | Optional; `${XDG_STATE_HOME:-~/.local/state}/dev-agent/runs` | Authoritative run directories and logs, outside managed repositories. |
| `results_root` | Optional; `<package location>/../../results` | For a source/editable install this is `<dev-agent repository>/results`. Set it explicitly for other installation modes. |
| `projects.NAME` | At least one project required | Multiple repositories can be configured in one file. |
| `repository` | Required | Existing non-bare Git working-tree root, not an arbitrary subdirectory. |
| `base_branch` | Required | Local branch name resolved as `refs/heads/NAME`, with an existing commit. Not an arbitrary SHA, tag or remote-tracking ref. |
| `worktree_root` | Required | Parent directory for per-run worktrees. |
| `env` | Optional; empty string-value table | Explicit environment for trusted setup/check processes. |
| `inherit_env` | Optional; empty name array | Explicitly inherit named invoking-process variables for trusted setup/check processes. |
| `preparation` | Optional; empty command array | Ordered setup, before agent execution. |
| `prerequisites` | Optional; empty table | Project-wide `env`, `paths` and `executables` arrays. |
| `verification_profiles` | Optional; empty table | Only `targeted` and `full` command arrays are accepted. |
| `verification` | Optional; empty command array | Backward-compatible `legacy` profile. |
| `agent` | Optional; defaults below | Codex-specific model, reasoning effort and timeout only; also used for investigation. |
| `atlassian` | Optional; required for investigation | GET-only Jira/Confluence provider and credential environment names. |
| `context` | Secure defaults | Mandatory automatic instruction hierarchy, additional architecture documents, implementation scope and resource limits for both workflows. |
| `investigation` | Optional | Bounded snapshot and prompt settings. |
| `github` | Optional; publication only | Explicit GitHub.com destination, literal source allowlist and CI timing; see [publication configuration](#configure-the-destination-and-source-manifest). |

Implementation task identifiers, project names and command names use 1–64 ASCII letters, digits,
underscores or hyphens and must begin with a letter or digit. Investigation instead
requires an uppercase Jira key matching `[A-Z][A-Z0-9_]{0,31}-[1-9][0-9]{0,15}`.
Command names must
be unique within each sequence. Environment names follow
`[A-Za-z_][A-Za-z0-9_]*`. All timeouts are integers from 1 through 604800 seconds;
booleans and fractional values are rejected.

Repository/storage paths expand `~` and resolve relative paths against the
configuration file's directory, not the invocation directory. Other environment
variables are not expanded in those paths. Use absolute XDG locations if overriding
XDG defaults. Canonical run/worktree roots must be outside every configured source
repository, outside each other and outside other Git repositories. Results must
not overlap canonical storage, managed repositories/worktrees or Git metadata.
If managing dev-agent itself, choose an external results root.

A dirty source checkout is allowed; its dirty state is recorded. Storage directories
are created for real runs, not dry-run. Existing branch, worktree, registration or
results collisions are refused. Results paths containing symlinks are refused.
Changing storage paths does not migrate prior data; restore the original settings
instead of trying to make old records describe a new location.

### Agent properties

| `agent` property | Default | Meaning |
| --- | --- | --- |
| `model` | Omitted; Codex default | Optional nonempty model string, passed through `--model`. User Codex config is ignored. |
| `reasoning_effort` | Omitted; Codex default | Accepted strings: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`. Actual model support is checked by Codex, not dev-agent. |
| `timeout_seconds` | `1800` | Maximum agent subprocess duration. |

There is no project option for arbitrary Codex flags, sandbox changes, network
access, approval escalation, provider credentials or additional writable roots.

### Command and prerequisite properties

Both preparation and verification use command arrays, as in the complete example.

| Command property | Required/default | Meaning |
| --- | --- | --- |
| `name` | Required | Safe command identifier used in records and log filenames. |
| `argv` | Required, nonempty string array | Executable followed by literal arguments; executable cannot begin with `-`. Empty/whitespace-only arguments and NUL are rejected. |
| `timeout_seconds` | `600` | Per-command timeout, independent of the agent timeout. |
| `cwd` | `"."` | Existing directory relative to the worktree. Absolute paths, `..` components, backslashes, drive prefixes and leading `~` are rejected. |
| `required` | `true`; preparation only | Whether a reported setup failure stops execution. Verification does not accept this property. |
| `env` | Empty table | Explicit string values for this child process. |
| `inherit_env` | Empty array | Additional named invoking-process variables to inherit. |
| `required_env` | Empty array | Effective environment variables that must have nonempty values. |
| `required_paths` | Empty array | Existing paths relative to the worktree, irrespective of `cwd`. |

`prerequisites.env` requires nonempty effective environment values;
`prerequisites.paths` requires existing worktree-relative paths;
`prerequisites.executables` requires PATH command names such as `uv` or `node`.
They are not command probes or installation instructions. Path prerequisites use
the same traversal/containment checks as command directories. Symlinks escaping
the worktree are rejected at execution time.

## Repository preparation

Before authorizing a real run:

1. Identify the intended repository root and local baseline branch. Ensure the
   branch contains the approved starting commit and that required source/lockfiles
   are committed. No automatic fetch, pull, baseline update or branch switch occurs.
2. Review source uncommitted/untracked changes. They are preserved in the source
   checkout and excluded from the new worktree. A different currently checked-out
   branch does not change which configured baseline is used.
3. Review repository and dependency scripts before authorizing their execution in
   preparation/verification. Identify needed language versions, workspace builds,
   generated fixtures and disposable external services.
4. Check that the configured verification commands represent the desired scope.
   Targeted and full are names chosen by the operator, not automatic test selection.
5. Provide a valid, committed root `AGENTS.md`, applicable nested instructions, architecture boundaries,
   package ownership, build/test commands and acceptance documentation. Make the
   relevant context explicit in the approved task as explained below.

Read-only checks you can perform on the synthetic project are:

```bash
git -C "$HOME/src/example-service" rev-parse --show-toplevel
git -C "$HOME/src/example-service" status --short
git -C "$HOME/src/example-service" rev-parse --verify 'refs/heads/main^{commit}'
```

`base_branch` accepts a local branch name, not a commit override. A chosen commit
must be reachable through the local branch you configure. dev-agent resolves and
records that branch's exact commit before creating `dev-agent/RUN-ID`.
Bare repositories and non-root repository paths are refused. Local/included Git
checkout filters are refused; Git LFS/filter-dependent workflows are not
transparently supported. Orchestrator Git operations disable hooks, fsmonitor and
external diff/text conversion helpers and ignore system/global Git configuration.
These protections do not sandbox your configured project commands.

Worktrees share Git configuration, refs and objects. They isolate working
directories but do not reproduce the source checkout's `.venv`, `node_modules`,
locally generated files, untracked `.env` or running test services. Use explicit
preparation rather than copying a developer's existing environment or credentials.
Submodule initialization is not performed; source snapshots reject submodules and
special source files. Worktrees are retained until explicit cleanup.

## Repository instructions and architectural context

**Root `AGENTS.md` and applicable nested instructions are mandatory for both
`implement` and `investigate`.** dev-agent discovers them from the exact selected
Git baseline, independently of Codex's own document loading. No list of nested
instruction files is required.

Codex's automatic project-document discovery remains disabled. The runners keep
`project_doc_max_bytes=0`, `--ignore-user-config`, `--ignore-rules`, untrusted project
configuration and disabled hooks/plugins/apps/memory/multi-agent features. Repository
text is engineering guidance, never permission to change the orchestrator's filesystem,
network, credentials, approvals, integrations or human-review boundaries.

### Mandatory hierarchy and precedence

The shared resolver inspects tracked Git tree entries and reads immutable blobs,
not active checkout files or another feature branch. Root instructions must exist.
Every discovered `AGENTS.md` must be a permitted regular committed file containing
nonempty UTF-8 text without NUL, escape/control characters (apart from tab/newline),
or other invalid content. Missing/unreadable/invalid required instructions stop
execution before Codex. Implementation planning failures return exit 2 without
creating a run; investigation records local context failure as exit 6. Because this version pre-supplies the full hierarchy, even
an invalid instruction in an unrelated subtree stops execution. No partial hierarchy
or silent truncation is accepted.

Applicability follows directory ancestry, root first and then progressively deeper
instructions. Deeper instructions refine engineering conventions only within their
own subtree. Siblings do not apply to one another:

| Source path | Applicable instructions |
| --- | --- |
| `frontend/components/Button.tsx` | `AGENTS.md`, `frontend/AGENTS.md` |
| `src/service/api/models.py` | `AGENTS.md`, `src/service/AGENTS.md` |
| `tests/unit/example.py` | `AGENTS.md`, `tests/AGENTS.md` |

For a backend change with tests, the backend source and test files have distinct
chains; their union includes root, backend and tests, without applying backend
instructions to unrelated test/frontend paths. Any depth is supported; byte/prompt
limits bound resource consumption rather than an arbitrary nesting limit. Directories
without their own instruction file inherit their ancestors. A new directory does
not require a newly invented `AGENTS.md`.

Only exact `AGENTS.md` names within the selected repository are discovered. Ancestor
home-directory instructions, `AGENTS.override.md`, untracked/dirty instructions and
unmerged branches are not automatically loaded. Symlink instruction files and
credential/control paths are rejected. Submodule instruction scopes are unresolved
and fail closed; they are not recursively fetched or inspected.

### Two-phase resolution and enforcement

Phase A resolves root instructions, discovers nested scopes, loads required
architectural documents, and constructs a manifest with path, directory scope,
baseline commit, Git blob ID, content hash, required/load status and source location.

Phase B uses **complete bounded pre-supply**: all discovered instruction bodies and
loaded architectural documents accompany that manifest in the initial prompt.
Root instructions are global within the repository; nested bodies are explicitly
labelled data for their own scope. Presence in the bundle never makes a sibling's
rules globally applicable. Pre-supply covers files the agent identifies later,
without requiring a second uncontrolled document-discovery mechanism.

Known configured Atlassian credential values are refused in repository context
for either workflow; this does not replace a general secret scanner.

The controller verifies that the complete bundle is included unchanged in the
prompt and records its digest, the prompt digest and supplied documents before
calling the agent. If the bundle cannot fit the configured limits, execution stops.
It does not rely only on an instruction to go read some files later.

For **investigation**, context resolution precedes Jira/Confluence retrieval.
The existing read-only namespace/sandbox is retained. After assessment schema and
citation validation, the resolver records applicable instruction chains for cited
repository files and checks those documents were supplied with matching hashes.
The response schema remains investigation-v1; instruction provenance is separate
controller-owned run metadata. Source/snapshot mutation still rejects the assessment.
Reports distinguish discovered, supplied and applicable-to-cited scopes. Uncited
reads and semantic obedience are explicitly unverified.

For **implementation**, the approved modification scope is established before
worktree creation. It defaults to repository-wide `"."`, safe only because the full
bounded instruction hierarchy is pre-supplied. Restrict it with trusted configuration
or repeatable CLI `--scope` flags:

```bash
uv run dev-agent implement TASK-123 --project example \
  --prompt-file tasks/TASK-123.md --scope frontend --scope tests --dry-run
```

`--scope` overrides the configured scope for that invocation. Entries are literal
baseline regular files or existing baseline directory scopes, not globs. Unknown,
unsafe, empty or duplicate scopes are rejected. New files/directories may be created
beneath an approved directory and inherit its baseline instruction chain. A literal
file scope authorizes that file, not a future directory with the same name.
Control directories (`.git`, `.codex`, `.agents`, `.aws`) and paths beneath them
cannot be explicitly approved, including when they contain tracked files. Planning
rejects these selections before Codex preflight or worktree creation.

After preparation and before Codex, the controller hashes worktree content for
paths in the Git index and non-ignored untracked files. An index entry remains in
scope when it matches an ignore pattern, including a staged addition. Required
baseline instructions and architecture documents must still match their baseline
hashes. A generated `AGENTS.md` affecting approved areas fails this gate even in
an ignored directory. Audit limits and unreadable or special paths fail closed.
The audit reads files through directory descriptors with symlink following disabled.

The byte budget measures regular-file content read during the audit. It limits
the cost of hashing eligible files and is separate from the worktree's disk
footprint. Ignored dependencies and generated files remain available for tests.
Their paths and file metadata are inspected within the entry limit to enforce
scope and instruction rules. For ignored regular files, that metadata includes
size, file identity and change timestamps. The controller does not read their
contents or calculate the total size of ignored files. Older implementation
audits can contain content hashes for ignored files. When comparing against
those records for publication, the controller retains the prior hash checks
under the same byte limit.

After Codex, source hashes and modes and ignored-path metadata are compared
against the captured state. Each changed path must be in an
approved scope and have its applicable baseline instructions supplied. Unknown
control/symlink scopes, changed HEAD, and any creation/modification/deletion/rename
of `AGENTS.md` reject the implementation as `instructions_failed` (exit 10), retaining
the worktree and skipping verification. Instruction-policy changes require a
separately human-reviewed baseline; this workflow cannot approve its own policy.

A final audit also checks changes left by verification commands, which remain
trusted processes outside the agent sandbox. Their changes are distinguished in
`final_changed_file_validation`; passing tests cannot erase a scope violation.
An incomplete final audit prevents a success claim. The root `.git` administration
entry is excluded from content hashing and worktree identity/HEAD checks remain
separate; this is not an exhaustive audit of shared Git metadata.

**Enforcement limit:** the Codex interface provides no reliable per-file tool-call
interception. This version enforces complete context availability and validates
persisted changes/citations after execution; narrower scopes are acceptance gates,
not additional OS write fences. An agent can attempt an out-of-scope edit before
its result is rejected, and transient edits restored before auditing cannot be
proven absent. Context can also be misunderstood or ignored semantically. The
controller does not claim every tool read/write was dynamically intercepted,
every uncited file was examined, or the model obeyed every convention. Worktrees,
sandboxes, source integrity checks and human review remain necessary.

### Context configuration and migration

```toml
[projects.example.context]
instruction_filename = "AGENTS.md"
require_root_instructions = true
enforce_nested_instructions = true
required_documents = ["docs/architecture.md"]
implementation_scope = ["."]
max_document_bytes = 65536
max_total_bytes = 262144
max_prompt_bytes = 1048576
max_audit_files = 200000
max_audit_bytes = 2147483648
```

Architectural documents differ from scoped instructions. They provide additional
repository context to **both** workflows and do not create new directory authority.
Only explicitly configured architectural files are loaded; there is no general
Markdown crawl. The synthetic architecture path above must be replaced with a
maintained file that actually exists at the selected baseline, or the array can
be empty. Root and discovered nested instructions remain mandatory either way.

| Property | Default / policy |
| --- | --- |
| `instruction_filename` | `"AGENTS.md"`; alternate names are rejected in this version. |
| `require_root_instructions` | `true`; `false` and non-booleans are rejected. |
| `enforce_nested_instructions` | `true`; cannot be disabled. |
| `required_documents` | Empty array; up to 32 explicit required baseline document paths. |
| `implementation_scope` | `["."]`; 1–64 unique literal baseline file/directory paths. CLI `--scope` overrides this selection. |
| `max_document_bytes` | `65536`; integer 1–1048576 for each instruction/architectural document. |
| `max_total_bytes` | `262144`; integer 1–2097152 across all loaded instruction/architectural bodies. |
| `max_prompt_bytes` | `1048576`; integer 1024–4194304. Investigation also applies its own `investigation.max_prompt_bytes`, using the smaller limit. Includes requirements and rendered manifest overhead; JSON encoding can increase size. |
| `max_audit_files` | `200000`; integer 1–1000000 file, symlink and directory entries per audit, including ignored metadata. |
| `max_audit_bytes` | `2147483648` (2 GiB); integer 1–17179869184 bytes of eligible regular-file content per audit, including any required comparison with older saved hashes. |

Large ignored environments can still consume the entry budget because their
metadata is checked. Narrower approved scopes do not reduce audit coverage.
Metadata changes can flag a path even when its content is unchanged. These
checks do not verify dependency integrity. Review resources before increasing
limits. Generated instruction files that intersect the approved scope require
explicit human resolution.

Legacy `context.instructions` remains accepted as a list of **required baseline
context documents**, now supplied to both workflows. An enumerated root/nested
`AGENTS.md` is redundant with automatic discovery and can be removed; other entries
can be moved to `required_documents`. Legacy `optional_instructions` remains optional
additional architectural context, with warnings for missing/unavailable files.
Listing any `AGENTS.md` there is rejected with a migration error: remove that entry,
rather than weakening the mandatory policy. These migrations are recorded in
provenance; optional architecture is never silently promoted to mandatory policy.

### Instruction provenance

Canonical `record.json` and exported `report.json` contain `instruction_compliance`:
policy strategy, baseline commit, discovered paths/scopes/hashes/load statuses,
which documents were supplied, bundle/prompt digests, applicable instruction sets,
architectural context, migration notes, warnings, failures and unverified areas.
Implementation also records approved scopes, the agent's changed-file validation
and final file validation. Investigation records applicability only for cited paths.

`repository-instructions.json` retains the exact supplied bodies/manifest separately
in canonical storage and exported results. Implementation additionally stores
`instruction-audit-before.json` and `instruction-audit-after.json`, containing
file hashes/types/modes, not file contents. Reports include a concise instruction-
compliance section without duplicating the instruction bodies. Historical records
without these fields remain readable and say `not_recorded`; they are not
retroactively certified. Reverification cannot repair missing original instruction
provenance or change the original outcome.

## Jira investigation

`investigate` retrieves a selected Jira Cloud issue and supporting Confluence Cloud
pages before launching Codex. No task prompt file is needed:

```bash
uv run dev-agent investigate TASK-123 --project example --dry-run
uv run dev-agent investigate TASK-123 --project example
```

Dry-run resolves the local baseline, checks configured architectural documents and
prints provider/credential **names**, limits, exact commit and proposed paths. It
makes no HTTP requests, requires no populated credentials, launches no Codex, and
creates no records/exports, branches or worktrees. Present named credential values
are checked only to refuse unsafe repository context; they are never printed.
Dry-run cannot establish login,
API permissions, model access or namespace readiness.

A real run persists the retrieved requirements **before** invoking the agent.
Required Jira failure stops execution. Optional linked-issue, Confluence or field-
discovery failures become explicit warnings, never invented content. Missing required
repository instructions/context or exceeded bounds stop before Codex; hierarchy
resolution precedes external retrieval. Investigation does not run preparation, project scripts or verification commands; project `env`, `inherit_env`
and command configuration are not supplied to its agent.

### Codex MCP replacement status

The existing Codex MCP registration does not automatically become available in
this controlled execution. A model-free native catalog probe failed with a transport
diagnostic and unknown authentication. Migration stopped before issue/page retrieval
or architectural replacement. See [MCP qualification](mcp-qualification.md) for the
installed-runtime findings, exact safety boundary and remaining gates.

There is no supported project MCP selection yet. Keep the existing REST setup if
using investigation now; do not add a hypothetical `[projects.NAME.mcp]` table,
remove `--ignore-user-config`, enable unrelated plugins or expose the complete home
in order to make an OAuth-only invocation appear to work. Separate REST credentials
remain required for that existing route; no new token store was introduced.

### Atlassian authentication

This standalone Python CLI does **not** reuse ChatGPT's Atlassian MCP session.
Configure a separate Atlassian email and API token through named environment
variables. Only `auth_method = "api_token"` is supported, using HTTPS Basic auth
with email/token. Password authentication and interactive OAuth are not implemented.
See Atlassian's [Basic auth documentation](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/).
This is a local/operator script integration; a distributable multi-user OAuth app
would require a separate design.

Create expiring, read-scoped tokens through your Atlassian account, using a dedicated
account with access only to relevant projects/spaces where practical. Scoped tokens
must use `cloud_id`: requests route through
`https://api.atlassian.com/ex/jira/CLOUD-ID` and
`https://api.atlassian.com/ex/confluence/CLOUD-ID`. The site URL remains the origin
for issue links and same-site page discovery. Use separate Jira and Confluence
product tokens via `token_env` and `confluence_token_env`; the latter defaults to
the former. Classic/unscoped API tokens omit `cloud_id` and use the site directly,
but offer a broader credential boundary. Review the current [API token setup and
routing requirements](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/).

Grant the account Browse Projects and visibility to the selected issue (including
issue-security restrictions), plus view access to the relevant Confluence spaces
and restricted pages. No issue edit, transition, comment, attachment upload or
Confluence write operation is implemented or needed. Token scopes do not grant
content permissions that the account itself lacks.

The read API allowlist and published scope references are:

| Operation | Endpoint | Scope reference |
| --- | --- | --- |
| Primary/directly linked issues | `GET /rest/api/3/issue/KEY` | Jira classic `read:jira-work`, or the complete granular read bundle in [Get issue](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get). |
| Acceptance field discovery | `GET /rest/api/3/field` | Classic `read:jira-work`, granular `read:field:jira`, `read:avatar:jira`, `read:project-category:jira`, `read:project:jira`, `read:field-configuration:jira`; [fields API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-fields/#api-rest-api-3-field-get). Disable discovery when using explicit field IDs if unnecessary. |
| Explicit remote links | `GET /rest/api/3/issue/KEY/remotelink` | Classic `read:jira-work`, granular `read:issue.remote-link:jira`, `read:status:jira`; [remote links API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/#api-rest-api-3-issue-issueidorkey-remotelink-get). |
| Confluence page body/version | `GET /wiki/api/v2/pages/ID` | `read:page:confluence`; [page API](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/#api-pages-id-get). |
| Issue-key documentation search | `GET /wiki/rest/api/search` with CQL | Classic `search:confluence`, granular `read:content-details:confluence`; [search API](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/#api-wiki-rest-api-search-get). |

Choose scopes for the configured reads in the account's token UI; consult each
endpoint's complete requirements rather than granting write/admin scopes to bypass
a 403. `read:issue:jira` alone is not the complete granular Get issue bundle.
Tenant policies may restrict API-token use further. No cloud-ID discovery call,
credential storage, token refresh or automatic authentication repair is provided.

Supply credentials from a secure local credential mechanism, or interactively
without putting values in shell history. For bash/zsh:

```bash
read -r "DEV_AGENT_ATLASSIAN_EMAIL?Atlassian email: "  # zsh syntax
export DEV_AGENT_ATLASSIAN_EMAIL
# For bash instead: read -r -p 'Atlassian email: ' DEV_AGENT_ATLASSIAN_EMAIL
# Tokens for either shell: hidden input through Python getpass.
export DEV_AGENT_JIRA_TOKEN="$(uv run python -c 'import getpass; print(getpass.getpass("Jira read token: "))')"
export DEV_AGENT_CONFLUENCE_TOKEN="$(uv run python -c 'import getpass; print(getpass.getpass("Confluence read token: "))')"
```

The `read` email line above is **zsh-specific**; use the commented bash alternative
in bash. Do not use shell tracing (`set -x`) around credentials. Clear these variables
when finished. Never put token values in TOML, command argv, task text or tracked
files. No `.env` loading occurs. Authentication failures report HTTP status and safe
diagnostics, without response bodies or Authorization headers.

### Investigation configuration

Add these tables to the existing synthetic `projects.example` configuration:

```toml
[projects.example.atlassian]
site_url = "https://example.atlassian.net"
auth_method = "api_token"
email_env = "DEV_AGENT_ATLASSIAN_EMAIL"
token_env = "DEV_AGENT_JIRA_TOKEN"
confluence_token_env = "DEV_AGENT_CONFLUENCE_TOKEN"
# Set the actual cloud ID when using scoped tokens:
# cloud_id = "YOUR-CLOUD-ID"
acceptance_fields = []
discover_acceptance_fields = true
search_confluence = true
confluence_page_ids = []
max_linked_issues = 5
max_confluence_pages = 8
timeout_seconds = 30
max_response_bytes = 2097152

[projects.example.context]
instruction_filename = "AGENTS.md"
require_root_instructions = true
enforce_nested_instructions = true
required_documents = ["docs/architecture.md"]
# Optional extra architecture only, never AGENTS.md:
optional_instructions = ["docs/testing.md"]
max_document_bytes = 65536
max_total_bytes = 262144

[projects.example.investigation]
max_snapshot_bytes = 134217728
max_snapshot_files = 20000
max_prompt_bytes = 524288
```

The [shared mandatory resolver](#repository-instructions-and-architectural-context)
loads the root and every nested `AGENTS.md` automatically, plus explicit required
architectural documents, from the selected baseline. The context rules/limits above
apply equally to both workflows. Optional architecture warnings constrain advisory
suitability; mandatory documents never silently degrade to optional context.

| Table/property | Default / accepted values |
| --- | --- |
| `atlassian.site_url` | Required HTTPS origin, with no path, credentials, query or non-443 port. Jira and Confluence must belong to this configured site. |
| `auth_method` | Only `"api_token"`. |
| `email_env`, `token_env` | Required credential environment **names**; may not overlap the inherited core agent environment. |
| `confluence_token_env` | Optional; defaults to `token_env`. |
| `cloud_id` | Optional ASCII letters/digits/hyphens, 1–64 characters; required for scoped-token gateway routing. |
| `acceptance_fields` | Empty array by default; explicit `customfield_NUMBER` IDs. At most 32 combined explicit/discovered fields may be used during retrieval. |
| `discover_acceptance_fields` | `true`; matches normalized field names exactly equal to Acceptance Criteria. |
| `search_confluence` | `true`; bounded issue-key CQL search, not arbitrary semantic search. |
| `confluence_page_ids` | Empty array; at most 20 numeric string IDs for explicit additional supporting pages. |
| `max_linked_issues` | `5`; integer 0–20. Direct linked issues only. |
| `max_confluence_pages` | `8`; integer 0–20. Configured, explicitly linked and searched page IDs are deduplicated in that order. |
| `timeout_seconds` | `30`; integer 1–120, HTTP socket-operation timeout, not a total investigation deadline. |
| `max_response_bytes` | `2097152`; integer 1024–8388608 per response. |
| `context` | Shared mandatory hierarchy and architecture configuration described [above](#context-configuration-and-migration). No per-subtree AGENTS.md list is needed. |
| `investigation.max_snapshot_bytes` | `134217728`; integer 1024–536870912 for included baseline file contents. |
| `investigation.max_snapshot_files` | `20000`; integer 1–50000 baseline entries, including omissions. |
| `investigation.max_prompt_bytes` | `524288`; integer 1024–2097152. Oversized requirements/context fail rather than being silently summarized. |

### Requirements and provenance

The provider retrieves the primary issue's title, description, type, status,
updated timestamp, direct issue links and configured/discovered acceptance fields.
Acceptance criteria can live entirely in the description. Original ADF structure
is retained; requirement units preserve source pointers and exact extracted text:
title, paragraphs/headings/code blocks, individual list-item paragraphs and
nonempty lines in plain custom-field strings. A heading may be contextual rather
than an acceptance obligation, and a paragraph can contain multiple obligations;
the agent must explicitly address them without dropping constraints. Source JSON
remains available for auditing that interpretation.

Supporting documentation is discovered from same-site numeric Confluence links
in issue content/direct linked issues, Jira remote links, explicit configured page
IDs, and bounded issue-key CQL results. Short links, cross-site pages, comments,
attachments, ancestor-page expansion and transitive crawling are not supported.
There is no automatic pagination or retry; caps, unavailable pages and disabled
search are reported. Optional omissions forbid unqualified `suitable` or
`already_satisfied` assessments. The primary ticket remains authoritative;
contradictions with linked issues/docs must be reported, not silently resolved.

`requirements-snapshot.json` records retrieval times, source identifiers/URLs,
full projected content, issue updated timestamps, page version/date when available,
content hashes, requirement units and omissions. Hashes cover stored sanitized
source objects excluding retrieval time, not original HTTP bytes. Absent revision
metadata is explicitly warned about; this is not a transactional Jira/Confluence
snapshot or proof that a page is current. Unneeded identity metadata is removed;
known credential values are redacted with a warning if encountered in retrieved
content. General personal/confidential content in requirements is not automatically
classified or anonymized.

### Read-only execution and baseline boundaries

The configured local branch is resolved to an exact commit without fetching,
switching branches or modifying the index. Committed blobs are materialized in
`runs_root/RUN-ID/repository/`, without a `.git` directory. Dirty checkout files
and unmerged feature-branch work are unavailable to the agent. No Git worktree or
implementation branch is created. No other branches are inspected, and remote
freshness is always `not_checked`; update your baseline manually under your own
change-control process before selecting it.

Snapshot files/directories are sealed and mounted read-only in a minimal Linux
Bubblewrap mount/PID namespace. The actual source checkout, parent process
credentials/environment and unrelated home directories are not mounted. Only
system runtime directories, TLS/DNS files, the native Codex executable, explicit
input evidence and existing Codex `auth.json` are exposed. Separate ephemeral
home/tmp directories and an agent-output directory allow the trusted CLI to run.
The source snapshot and canonical controller reports are not writable by agent
commands. File-based Codex login is mounted read-only for the CLI itself; this is
not credential-free execution, and does not prove that Codex's own login material
is inaccessible to every read tool. Protect the Codex account accordingly.

Codex runs with `--sandbox read-only`, approval `never`, user config/rules ignored,
web search/integrations/plugins/hooks/memory/multi-agent disabled and automatic
project-document discovery disabled. Agent tool networking remains restricted by
Codex's sandbox; the CLI still needs host networking for model calls. Bubblewrap
is an additional filesystem/PID boundary, **not** a separate network air gap or a
VM. A compatible native Codex binary, file-based login and usable namespaces are
mandatory; probe failure stops without a permissive fallback. Dependencies/tools
under the source checkout's virtualenv or user home are not mounted. This workflow
uses static file/search evidence and deliberately executes no project tests.

Before/after fingerprints cover the current source HEAD/index, tracked and
nonignored untracked contents/modes, selected baseline ref, and snapshot contents/
modes. Ignored source files and all Git metadata are not exhaustively audited.
Any detected source/snapshot mutation, including a concurrent human edit, rejects
the assessment as `security_failed`. Fingerprints cannot prove the absence of a
transient change that was restored, or prevent an external process modifying the
checkout. They supplement read-only mounts; they are not the sole enforcement.

Atlassian credentials stay in the Python provider and are never passed as Codex
argv/environment/prompt values. HTTPS requests refuse redirects and do not inherit
proxy variables. Credential-bearing repository content is refused when matching
known configured credential values; this is not a general-purpose secret scanner.
Both external and repository text remain untrusted data, never orchestration
policy. Sending repository/requirements content to the configured Codex model
still requires your organization's approval.

### Investigation results and schema

Each investigation receives a unique run ID. The canonical `record.json` has
`kind = "investigation"` and schema version 2, sharing existing run lookup/locking.
It records exact baseline, provider metadata (credential **names** only), timestamps,
agent status/exit/timing, source integrity, limitations and outcome. Canonical
artifacts remain authoritative; exported `report.json` is a projection.

```text
results/TASK-123/RUN-ID/
├── investigation.md
├── investigation.json
├── investigation-schema.json
├── requirements-snapshot.json
├── sources.json
├── context.json
├── repository-instructions.json
├── agent-events.jsonl
├── agent-output.md
├── agent.stderr.log
├── run.log
└── report.json
```

The sealed repository copy remains under canonical run storage, not duplicated
in results. `sources.json` records external/context provenance and the baseline
file manifest (including omissions and hashes). Raw output and events remain
separate from the concise human report. Pre-agent failure cannot produce artifacts
that were never retrieved/executed. Empty event/output placeholders may be present;
`investigation.json` exists **only** for an accepted assessment. Invalid output is
preserved in `agent-output.md`, never repaired or filled in by the controller.

The versioned [investigation-v1 JSON schema](../src/dev_agent/investigation/investigation-schema.json)
is shipped in the package and copied into run evidence. It is a closed object:

- Identity: `schema_version: 1`, `task_id`, `baseline_commit`, `executive_summary`.
- `criteria`: every preserved requirement ID/text exactly once, `status`, `evidence`,
  `tests`, `explanation`, `gaps`, `uncertainties`, `confidence` and `runtime_verified: false`.
- Evidence: baseline `path`, `line_start`, `line_end`, `symbol`, verbatim `quote`,
  and `kind` (`static_implementation`, `test_coverage`, `documentation`).
- Findings: `existing_implementation`, `missing_functionality`,
  `architectural_constraints`, `testing_assessment`, `dependencies_and_risks`,
  `contradictions`; each item has `text`, `source_ids`, `repository_evidence`.
- Advisory delivery: `delivery_factors` (requirements, architecture, implementation,
  testability, database migrations, authentication, external infrastructure,
  production risk, cross-feature dependencies and active development conflicts),
  `suitability`, `suitability_explanation`, `constraints`, `recommended_next_action`.
- Fixed evidence limits: `executed_verification: []`,
  `remote_freshness: "not_checked"`, `other_branches_examined: []`.

Criterion statuses are `satisfied`, `partially_satisfied`, `not_satisfied`,
`unable_to_verify`, `not_applicable`. Confidence is `high`, `medium` or `low`.
Suitability is `suitable`, `suitable_with_constraints`, `requires_human_decision`,
`blocked`, or `already_satisfied`. These are advisory, never instructions to close
Jira or begin implementation. Active-development conflicts generally remain
uncertain because other branches and remote freshness are not investigated.

The controller validates schema, required coverage, identity, known source IDs,
file existence, cited line ranges/quotes/symbols and advisory consistency.
`satisfied` requires implementation evidence; a test citation alone cannot satisfy
it. Unknown/missing fields, unsupported claims of executed tests, ungrounded quotes
or inconsistent decisions reject the entire assessment. Successful validation
proves structural consistency and citation existence, **not semantic correctness**,
requirement completeness or runtime behaviour. Read the findings and source
snapshot before making a delivery decision.

Use existing interfaces for either run kind:

```bash
uv run dev-agent runs list
uv run dev-agent runs show RUN-ID
uv run dev-agent inspect RUN-ID
uv run dev-agent results RUN-ID
uv run dev-agent cleanup RUN-ID
```

Inspection shows investigation outcome/phase, baseline, snapshot integrity, source
mutation evidence, limitations, suitability and results path. `verify` refuses
investigation records; independent verification still applies only to retained
implementation worktrees. There is no automatic conversion from findings to an
implementation prompt. Multiple investigations of the same issue retain distinct
IDs/directories without overwriting historical evidence.

Cleanup requires explicit confirmation and removes **only the sealed snapshot**;
reports, requirements, context and agent evidence remain. Changed or unknown
snapshot integrity requires a separate `--force` plus confirmation. This does not
clean or modify the managed source checkout. Inspect canonical/exported evidence
before deliberately discarding an altered snapshot.

## Environment preparation

Preparation commands execute sequentially in configuration order. Each command
starts a separate subprocess without a shell. Pipes, `&&`, redirections, `$VAR`
and command substitution are not interpreted by dev-agent. Configuring a shell,
interpreter or repository script explicitly is still trusted executable policy;
its contents can perform arbitrary operations outside the agent sandbox.

Before each setup command, dev-agent validates worktree identity, the command's
working directory and declared command requirements. Project-wide path/env/tool
prerequisites are checked after setup, so setup can create required files first.
Project `inherit_env` requirements still apply to preparation. After setup, selected
verification command requirements and directories are also checked before Codex;
prerequisites are rechecked after the agent and before verification commands.

Each performed command records start/completion timestamps, duration, exit status,
stdout/stderr log paths, timeout and status. Unperformed commands stay `not_run`.

| Result | Behaviour |
| --- | --- |
| Required setup command reports failure or timeout | Stop setup, do not start an agent implementation session, record `environment_failed`, preserve the worktree and export results; normally exit 6. Codex help/version preflight has already occurred. |
| Optional setup command reports failure | Continue when prerequisites allow; record `optional_failed`. Otherwise passing implementation/checks become `passed_with_warnings`, exit 4. |
| No setup commands | Preparation is `skipped`; prerequisites are still checked. |
| Declared prerequisites missing | Record their names/paths/tools as evidence; do not treat unavailable checks as passes. |
| Interruption or orchestration exception | Stop remaining steps; attempt to persist interrupted/error evidence. Optional status does not override an orchestration error. |

Timeout handling sends termination to the child process group, then kills remaining
descendants after a short grace period. It does not retry or repair. Ctrl-C/SIGTERM
attempt similar interruption handling. SIGKILL, power loss or storage failure can
prevent final records and leave processes/artifacts requiring manual inspection.

### Environment scope

Children begin with the reduced environment: `PATH`, `HOME`, `USER`, `LOGNAME`,
`LANG`, `LC_ALL`, `TERM` and `CODEX_HOME`, when present. No blanket inheritance of
API keys, cloud credentials, Git overrides, `VIRTUAL_ENV` or `PYTHONPATH` occurs.
Trusted commands can receive additional values through configuration:

1. Copy the union of project and command `inherit_env` names from the invoking
   process when present.
2. Apply explicit project `env`, then explicit command `env`; explicit values win
   over inherited values.
3. Replace `{worktree}` in explicit env values with the worktree's absolute path.

There is no other interpolation in env values, argv or working directories.
Required/inherited names must have nonempty effective values. Child `export`
commands do not alter the orchestrator or later child processes. Configure values
explicitly instead. Project/command env additions are **not given to Codex** and
their values are not written into metadata; variable names and configuration hashes
are recorded. Credentials in argv would be recorded and verification argv would
also be included in the agent prompt, so never put secrets there.

For a frontend project, reviewed preparation might use `argv = ["npm", "ci"]` and
then a workspace build, with `cwd = "frontend"` if that is where the project keeps
its lockfile. Those commands are examples to configure, not automatic integrations.
Installation may need network access and execute dependency scripts; the owner
must explicitly authorize that through trusted configuration.

For an existing project that requires database integration tests, an additional
full-profile check can inherit only test-specific settings:

```toml
[[projects.example.verification_profiles.full]]
name = "database-tests"
argv = ["uv", "run", "--locked", "pytest", "tests/integration"]
inherit_env = ["TEST_DATABASE_DSN"]
required_env = ["TEST_DATABASE_DSN"]
```

Use this only if your project understands that variable and the connection points
to disposable test-only infrastructure. Supply its value locally; no credentials
are included in this example. dev-agent does not provision PostgreSQL, validate
whether a DSN is production, load `.env`, or tear down external services. Operators
must prevent production access and manage test-service lifecycle independently.

## Verification

### Profiles and check outcomes

`--verification targeted` and `--verification full` select exactly the configured
command arrays. There is no inference from the task description. Without a flag,
`targeted` is selected if configured; otherwise the legacy `verification` array is
used. Full is never selected automatically. Requesting an unconfigured targeted or
full profile fails before creating a worktree. An empty selected sequence is
`unverified`, not a successful verification.

The legacy array remains available through `--verification legacy` and
`verify --profile legacy`. It is not automatically relabelled as targeted or full.
The default profile choice for `verify` follows the current project configuration,
not necessarily the profile selected for the original implementation.

```bash
uv run dev-agent implement TASK-123 --project example --prompt-file tasks/TASK-123.md --verification targeted
uv run dev-agent implement TASK-123 --project example --prompt-file tasks/TASK-123.md --verification full
```

Each real `implement` command above starts a different execution/worktree. Use
`verify` below to check retained work instead of launching another implementation.
All selected checks run sequentially after a returned agent result, even if the
agent failed or an earlier check returned nonzero. Interruption, invalid worktree
identity or orchestration errors stop the sequence. Missing declared prerequisites
are distinguished from command failures and timeouts. A nonzero test process is
otherwise a generic command failure; dev-agent does not diagnose its output as an
environment problem or an implementation regression automatically.

A successful Codex result requires zero exit status, a completed turn event, no
failed/error events and a nonempty final response. Initial run `passed` additionally
requires successful checks and acceptable final source/inspection/export state.
Agent failure takes precedence over ordinary check outcomes in the initial run;
interruption/orchestration failures are recorded separately. Command exit success
is not proof that task acceptance criteria hold; inspect logs for tool-internal
skips and review the code. No automated repair or independent AI review follows.

### Independent reverification

Use a real ID printed by the original run; `RUN-ID` below is a placeholder.

```bash
uv run dev-agent verify RUN-ID --profile targeted
uv run dev-agent verify RUN-ID --profile full
uv run dev-agent verify RUN-ID --profile legacy
# Explicitly allow the configured setup to run again on retained work:
uv run dev-agent verify RUN-ID --profile targeted --prepare
```

`verify` never invokes Codex. It reuses and validates the retained worktree, refuses
cleaned/missing/invalid or actively locked runs, and appends a uniquely identified
attempt. It uses the **current loaded configuration**; record hashes and command
snapshots identify which settings applied. Preparation is off by default.
`--prepare` may modify the retained environment/source through trusted commands;
that setup is recorded separately for the attempt.

Original agent events/output, original verification fields and original outcome
remain unchanged. Each new attempt has its own profile, source state, prerequisites,
logs, timestamps and outcome. A later full-profile failure does not replace an
original targeted pass, nor does a later pass retroactively verify earlier source.
The `verify` exit code reflects the new attempt, with export errors reported
separately. Use `runs show` or `inspect` to distinguish original and latest results.

### Source state and baseline comparison

Snapshots capture HEAD, index state, tracked file contents/modes, symlink targets
and nonignored untracked contents. They do not follow source symlinks to hash
unrelated files. Reports record fingerprints before/after checks and whether source
has changed since the original implementation snapshot. Passing checks that modify
source produce a `source_changed` verification status and `verification_failed`
outcome, rather than claiming one stable state was verified.

Ignored environments/caches are excluded, with an ignored-dirty indicator recorded.
Dependencies, services, system tools and all environment values are not fully
fingerprinted. Avoid concurrent human/tool writes while verifying or cleaning up.

The configured baseline SHA is provenance, **not a baseline test result**. The CLI
does not run the same suite against an unmodified baseline for comparison. A
failure cannot be attributed to the implementation solely because it occurred in
its worktree. Use explicit evidence or retain an unresolved generic failure.
Schema-1 runs lacking an original source fingerprint record comparison as unknown;
changed filenames alone cannot reconstruct their original contents.

## Execution results

The layout below describes implementation runs. The separate [investigation
layout](#investigation-results-and-schema) uses the same task/run path and canonical
authority, with `investigation.md` instead of an implementation summary.

Three locations have different purposes:

| Location | Authority and lifecycle |
| --- | --- |
| `runs_root/RUN-ID/` | Authoritative canonical metadata/evidence; retained after cleanup. |
| `results_root/TASK-ID/RUN-ID/` | Human-readable/exported projection; does not replace canonical storage. |
| `worktree_root/RUN-ID/` | Implementation and its prepared environment; retained until explicit cleanup. |

For source/editable installation, the default results root gives:

```text
<dev-agent repository>/results/
└── TASK-123/
    └── RUN-ID/
        ├── summary.md
        ├── report.json
        ├── agent-events.jsonl
        ├── agent-output.md
        ├── agent.stderr.log
        ├── run.log
        ├── preparation/
        │   ├── check-python-environment.stdout.log
        │   ├── check-python-environment.stderr.log
        │   └── ATTEMPT-ID/       # when reverification setup has logs
        └── verification/
            └── ATTEMPT-ID/
                ├── report.json
                ├── check-unit-tests.stdout.log
                └── check-unit-tests.stderr.log
```

Names vary with configured commands. Unperformed commands may have no log files;
empty agent artifacts do not imply the agent ran. Historical original check logs
may be exported under `verification/original/`. The safe task identifier determines
the task directory, and unique run IDs separate repeated tasks. Other runs' results
and symlink destinations are not silently overwritten.

Canonical `record.json` contains project/task identity, baseline branch/SHA, source
state, configuration hash, worktree/branch paths, timestamps, preparation,
prerequisites, agent result, original checks, verification attempts, changed files,
outcome and cleanup information. Canonical logs use `final-response.md` for the
agent output, `preparation/check-NAME.*.log` for initial setup and
`verification/ATTEMPT-ID/check-NAME.*.log` for checks. Reverification setup is stored
inside its canonical attempt directory. Older checks retain their original
canonical top-level filenames.

The summary reports original execution and profile-specific attempts, a short
unreviewed agent note, changes, log links, failures and human review boundaries.
It does not embed the complete transcript. Root export `report.json`/`summary.md`
and the run log can be refreshed after verification/cleanup; completed attempt
reports and agent/check artifacts are preserved. JSON/summary replacement is atomic
per file, not transactional across the whole directory. An export may be stale or
partial after a storage error: consult canonical state and `results_error`.

```bash
uv run dev-agent runs list
uv run dev-agent runs show RUN-ID
uv run dev-agent inspect RUN-ID
uv run dev-agent results RUN-ID
```

`runs list` identifies investigation records with `kind = "investigation"` (older
records default to implementation). For implementation runs it retains original outcomes and shows the latest verification outcome
separately. `inspect` performs read-only Git inspection and displays original/current
status, worktree/results paths, setup/prerequisite details, source changes and the
latest attempt; it requires a valid worktree unless already cleaned. `results`
prints a directory only and never launches an editor.

Existing schema-1 runs remain in their original canonical locations without
migration during lookup. An older run may have no export: `results` reports that
absence, while `runs show` remains available. Explicit reverification can create
an export and append history. Restore original source/worktree/run/results settings
when managing existing runs; manually moved or replaced registrations need review.

## Ticket delivery

**Live Jira-driven delivery is blocked on Codex MCP qualification.** The current
checkout has no secure, qualified Codex Atlassian MCP retrieval adapter. The
isolated catalogue previously reported `notLoggedIn`; transport/OAuth isolation,
real read-tool semantics and trustworthy source capture remain qualification
requirements. See [the preserved qualification evidence](mcp-qualification.md).
`deliver` does not use the REST investigation provider, request new tokens,
inherit unrestricted user integrations, or relax the sandbox to compensate.
There is no configuration switch that declares MCP qualified.

The new command accepts project and ticket identity directly:

```bash
uv run dev-agent deliver TASK-123 --project example --draft --dry-run
uv run dev-agent deliver TASK-123 --project example --draft --confirm --watch
```

Both targeted and full profiles must be nonempty and GitHub publication must be
configured. Baseline root/nested instructions and mandatory architecture are
resolved before intake. Dry-run validates local prerequisites, reports the MCP
blocker, performs no external requests and creates no run/worktree. A real
invocation creates a canonical delivery record and private results containing
the blocker, then stops before Codex, worktree creation, GitHub authentication or
publication. Exit code `13` means delivery was not fully successful, including
`mcp_unavailable`, waiting for confirmation, failed review/checks and incomplete CI.
Preflight/configuration errors retain exit `2`.

### Implemented downstream checkpoints

These checkpoints are implemented and exercised with **synthetic MCP evidence**
in tests. They cannot currently be reached from live CLI ticket retrieval; they
are not evidence that the MCP integration works:

1. Validate preserved Jira content/hash/provenance and the existing versioned
   baseline investigation assessment. Ambiguous/blocked assessments stop; a claim
   of already-satisfied requirements does not launch implementation.
2. Generate a ticket-specific Markdown brief from exact requirement units,
   baseline code/test citations, architecture findings, dependencies and constraints.
   AGENTS.md bodies are supplied separately, rather than copied into the brief.
3. Persist the brief, structured plan, requirements, sources, baseline instruction
   bundle and artifact hashes. Reserve the child implementation ID before starting it.
4. Invoke the existing `implement` engine with that ID and the exact planned
   baseline, using its preparation, worktree, instruction and targeted verification
   machinery. Run the configured full profile separately. No automatic repair loop
   or full-verification waiver is available through `deliver`.
5. Run a separate ephemeral Codex invocation against a sealed snapshot of the
   candidate source, inside the existing read-only Bubblewrap boundary. It receives
   requirements/brief and the baseline instruction bundle, not the implementation
   agent transcript, GitHub credentials or MCP access. It runs no application tests.
6. Validate review JSON against a closed versioned schema: exact task/baseline/source
   fingerprint, every requirement ID, source-backed citations, verdict and findings.
   Unknown coverage, blocking findings, invalid output, source/snapshot mutation or
   missing retained review evidence blocks publication. Static review is advisory,
   not proof of behaviour or human approval. Failed findings on unchanged source are
   not automatically re-reviewed until a random pass appears.
7. Use the existing publication preview, explicit manifest, exact source gates,
   commit/push/draft creation and recovery. A delivery-managed child also enforces
   its independent review gate when published through the individual command.
8. Observe exact-commit CI through the existing monitor. `--watch` polls; without
   it, retrieve one observation. Missing, neutral/skipped, cancelled, failed,
   unavailable or timed-out CI cannot produce `delivered`.

`--confirm` authorises **only commit, push and draft PR creation**. It does not
approve ambiguous requirements, override instructions, waive verification/review,
change authentication, merge or deploy. Without it, the downstream pipeline stops
after review and publication preview. Existing repository workflows may have
external effects when pushed; the publication workflow warnings still apply.

### Records, progress and resumption

Stage progress goes to stderr; stdout contains a final JSON summary with execution
ID, Jira key, implementation run ID, generated brief location, verification/review
status, commit SHA, PR URL, CI status and blockers. A blocked intake has no generated
brief or implementation ID; none is fabricated.

Canonical delivery records reference a separate canonical implementation record.
The parent owns orchestration checkpoints and planning evidence; the child remains
authoritative for implementation, verification, review and publication evidence.
The delivery export follows `results/TASK-ID/DELIVERY-ID/` and contains
`summary.md`/`report.json` plus generated planning artifacts when available.
The implementation export keeps its own run ID and includes review/publication
evidence. Review raw output/events remain in canonical storage; accepted structured
findings and source hashes are exported. No results are uploaded automatically.

```bash
uv run dev-agent runs show DELIVERY-ID
uv run dev-agent inspect DELIVERY-ID
uv run dev-agent results DELIVERY-ID
uv run dev-agent deliver TASK-123 --project example --draft \
  --resume DELIVERY-ID --confirm --watch
```

Use the linked implementation ID with `verify`, `inspect`, `publish`, `ci` and
explicit `cleanup` for debugging. Delivery evidence itself is retained; the
parent cleanup command refuses to remove it or silently clean the child.

Resumption validates project/ticket identity, original configuration, baseline,
artifact hashes and child linkage. An execution lock excludes concurrent delivery
invocations. It does not silently adopt a historical implementation by ticket key,
regenerate changed briefs, start another implementation agent, or invent missing
history. Incomplete/failed implementation checkpoints require explicit inspection;
there is no unsafe automatic restart after an uncertain agent outcome. Finished
source with fresh checks can proceed. Interrupted read-only review may resume as a
new recorded attempt; accepted failing findings on unchanged source remain a blocker.
Publication recovery reuses the existing commit and PR checks rather than creating
another commit or force-pushing. Resuming an already published execution to inspect
CI is read-only and does not require renewed publication confirmation.

Starting without `--resume` creates a **new execution**, even for the same ticket;
use the printed execution ID after interruption. Baseline/configuration changes
require a new reviewed execution. Changed source requires new explicit verification
and review; old evidence never certifies it. The existing caveat for interruption
between Git commit creation and its durable publication checkpoint still applies:
manual inspection may be required, and a new commit is not automatically guessed.

The operational next step is the narrow MCP transport/OAuth/provenance qualification
already documented, then wiring the qualified intake into this orchestration seam.
Fake intake in regression tests is not an available provider or a qualification bypass.
Live delivery remains blocked at intake until this qualification passes.

## Draft publication and CI

Publication is optional and privileged. It uses the installed **GitHub CLI (`gh`)**
with existing authentication; it never starts a login flow. On Ubuntu, install
`gh` from a reviewed package source (for example `sudo apt install gh`); follow the
[GitHub CLI installation guidance](https://github.com/cli/cli#installation) when a
newer package is needed. Authenticate yourself outside dev-agent with
`gh auth login --hostname github.com`, then check `gh auth status --hostname github.com`.
No token needs to be added to dev-agent configuration. Stored gh authentication,
`GH_CONFIG_DIR`/`XDG_CONFIG_HOME`, and existing `GH_TOKEN`/`GITHUB_TOKEN` environment
authentication are used only by the trusted publication process, never Codex.
The account/token needs repository contents read/write and pull-request read/write
access, plus read access to checks, commit statuses and Actions for CI inspection.
Organisation policy and branch protection can still reject a push or PR. dev-agent
checks account/repository permission but does not modify those policies or request
additional scopes automatically.

The installed CLI must support `gh auth status`, `gh api`, `gh auth git-credential`
and `gh pr create --draft --body-file`. Structured CI retrieval uses `gh api`,
so JSON output from `gh pr checks` is not required. The implementation is tested
with a fake CLI; live GitHub publication has not been qualified by the automated suite.

### Configure the destination and source manifest

Add this to external configuration, substituting the actual intended GitHub.com
repository and **literal** source files/directories:

```toml
[projects.example.github]
repository = "example-org/example-service"
allowed_paths = ["src", "tests", "docs", "pyproject.toml", "uv.lock"]
ci_timeout_seconds = 600
poll_interval_seconds = 10
```

`github` is optional. Once present, `repository` and nonempty `allowed_paths` are
required. Paths are relative, without traversal; `.` and glob patterns are refused.
Timeout and poll interval are integers from 1 to 604800 seconds. Only GitHub.com
is supported. `base_branch` remains the project's configured local baseline name;
there is no task-controlled destination/base override. The origin must have one
fetch URL and at most one push URL, both matching the intended repository.
Credential-bearing URLs, local transports, URL rewrites and unsupported local
Git transport/helper configuration are refused. HTTPS and ordinary GitHub SSH
origin formats are recognised; publication itself pushes over HTTPS using the
existing `gh auth git-credential` helper without modifying Git configuration.
Local Git `user.name` and `user.email` must already be set in the managed repository.
Global Git configuration, hooks, automatic signing and checkout filters are not used.

### Verification and instruction gates

A publishable run must be a finished implementation with successful Codex
completion, an intact retained worktree on its recorded task branch, a known
baseline and recorded passing mandatory instruction/scope checks. Current files
are audited again against the baseline instruction bundle and original approved
scope. Missing historical evidence is a blocker, not an implicit pass.

Run targeted and full verification explicitly when needed:

```bash
uv run dev-agent verify RUN-ID --profile targeted
uv run dev-agent verify RUN-ID --profile full
```

A passing **targeted** attempt is mandatory. Legacy checks never implicitly count
as targeted or full. Checks must be configured, actually pass, have matching
before/after source fingerprints, and leave source unchanged. The recorded
configuration digest must match current configuration: after adding/changing
publication configuration, reverify under that configuration. A later passing
attempt certifies only its source state; old failures and original outcomes stay
in the record. This does not prove the targeted profile has adequate coverage;
the owner must configure checks relevant to the task.

Full verification must pass unless the human separately supplies both
`--allow-incomplete-full` and `--full-verification-reason TEXT`. Failed, stale,
unavailable and unexecuted full checks remain visibly distinct from a targeted
pass. The acknowledgement is persisted and prominently placed at the top of the
PR body. There is no override for targeted failures, stale targeted evidence,
instruction failure or worktree-integrity failure.

The remote base branch is checked without fetching or changing local refs. A
remote commit different from the implementation baseline blocks publication
until `--acknowledge-stale-base` is supplied after review. Its ancestry is not
inferred if the remote object is not local. No merge or rebase is performed.

### Preview and confirm

```bash
uv run dev-agent publish RUN-ID --draft --dry-run \
  --title "Use consistent configuration fields" \
  --summary "Align configuration fields with the approved project conventions."
```

Preview checks authentication, push permissions, remote branch/PR identity,
workflows, changes and verification. It prints the complete proposed file
manifest, ignored/excluded paths, PR body, blockers and `plan_sha256`.
It does not stage files, create commits/branches, push, create PRs or persist a
publication plan. Omitting both `--confirm` and `--dry-run` is also a read-only
preview. This deliberately does **not** call `gh pr create --dry-run`, whose
[documented behaviour can still push changes](https://cli.github.com/manual/gh_pr_create).

After reviewing the diff, manifest, title/body, verification and workflow policy:

```bash
uv run dev-agent publish RUN-ID --draft --confirm \
  --title "Use consistent configuration fields" \
  --summary "Align configuration fields with the approved project conventions." \
  --plan-sha256 REVIEWED-PLAN-SHA256
```

The optional plan hash refuses changed preview identity. Repeat all preview
options on confirmation, including any full-verification or stale-base
acknowledgements. Without a hash, `--confirm` approves the currently computed
manifest subject to all gates. Titles are prefixed with the task identifier.
Default title/body text is deliberately modest; use reviewed title/summary text
for a useful explanation without uploading task files or the Codex transcript.

`--file PATH` can be repeated to explicitly identify the complete manifest.
Partial publication is refused while other nonignored source changes remain:
verification of a larger working tree does not prove the selected subset works.
Newly untracked source is included only within allowed paths. Additions,
modifications, deletions and ignored files are identified separately. Secrets,
`.env` files, local credential/config files, tasks, logs, results and common build
artifacts cannot be staged automatically. Symlinks, binary files, special files
and files over 10 MiB require manual publication. Potential-secret detection is
heuristic and is **not** proof of absence; review every file. A directory allowlist
is a reviewed source policy, not automatic approval of arbitrary generated files
inside it. Remove unwanted output and reverify before confirmation.

Publication stages only explicit literal paths, checks the exact staged list,
content hashes and modes, then creates one ticket-prefixed commit. Existing staged
changes, unexpected commits and Git operations are refused. The retained source
checkout's active branch and dirty files are preserved. Only the recorded task
branch is pushed, without force; baseline branches, tags and other remotes are
not pushed. No repository or global authentication configuration is changed.
A draft PR targets `base_branch`. No automatic merge or deployment command exists.

**Review existing workflows before confirming:** push/PR events may trigger
owner-configured jobs with external effects, including deployment. dev-agent does
not create, edit, dispatch or cancel workflows, and cannot neutralise effects of
existing repository automation. The plan inventories baseline workflow files,
recognises common `pull_request`, `push` and `workflow_dispatch` syntax and flags
conditions/draft references. This is conservative static inspection, not a YAML
interpreter or proof a workflow will run. Anchors, complex YAML, branch/path
filters, disabled Actions and other policy require human review.

### CI observation and recovery

After draft creation, publication polls existing checks until completion or
`ci_timeout_seconds`. Inspect again without changing Git or creating another PR:

```bash
uv run dev-agent ci RUN-ID
uv run dev-agent ci RUN-ID --watch
```

Observations include the published commit, observed PR head, check names/states,
URLs and available start/completion timestamps. Check runs and commit statuses
are retrieved for the exact published SHA. A changed PR head yields
`commit_mismatch`; earlier CI does not certify it. Statuses distinguish `pending`,
`passed`, `failed`, `cancelled`, `skipped`, `no_checks`, `no_checks_configured`,
`unable_to_retrieve` and `monitoring_timeout`. No detected workflows and no
reported checks yields `no_checks_configured`; workflows with no reported checks
yield `no_checks` (watch waits for possible asynchronous registration). Neither is
a pass. Neutral/skipped checks are not clean passes. Timeout stops monitoring
without cancelling remote jobs. Draft conditions can prevent checks indefinitely.
CI commands currently require the retained worktree and original project mapping.

Canonical run metadata remains authoritative. Publication adds an event history,
approved plans/manifests, commit/tree SHA, destination, PR number/URL/draft status,
verification acknowledgement, workflow inventory, failures and append-only CI
observations. Original implementation/verification evidence and outcomes are not
replaced. The existing export receives:

```text
results/TASK-ID/RUN-ID/
  summary.md
  report.json
  publication/<attempt-id>/
    plan.json
    approved-files.json
    pr-body.md
```

`report.json` includes publication and CI history; summaries show the latest
status without claiming original full verification passed. `inspect` also shows
publication evidence. Export failures leave canonical evidence intact.

If push fails, the local commit is retained. If push succeeds but PR creation
fails or its response is lost, rerun preview and the same confirmed command.
The orchestrator reconciles the remote commit and searches for an existing PR
before creating one; it reuses its commit and never force-pushes. A closed,
non-draft, wrong-base, ambiguous or changed-head PR requires manual resolution.
Unexpected remote branch commits are refused. Failures during staging/commit or
an interruption between commit creation and canonical persistence can require
manual index/commit inspection; there is no automatic reset or unstage operation.
Do not manually push additional commits and expect old approval to cover them.
New source changes require a separately reviewed implementation/publication flow.

The original historical run stays ineligible for direct publication when it
lacks instruction-supply evidence. For an eligible retained worktree, the
[legacy adoption workflow](quickstart.md#adopt-a-retained-legacy-implementation)
records a separate current audit, targeted and full verification, independent
review, and explicit acceptance of the provenance gap. Publish the resulting
adoption ID using the usual draft preview and confirmation.

## Security

- **Implementation Codex policy:** `workspace-write`, approval policy `never`, shell network and
  web search disabled, no additional writable roots, default temporary writable
  roots excluded, no login shell. User config/rules and normal project discovery
  are restricted as described [above](#repository-instructions-and-architectural-context).
  No permission bypass flags are used. The installed Codex binary and system/managed
  configuration remain trusted; dev-agent does not implement the sandbox itself.
- **Investigation Codex policy:** `read-only` within a required Bubblewrap
  filesystem/PID namespace, explicit baseline context and no Atlassian credentials;
  see [read-only boundaries](#read-only-execution-and-baseline-boundaries) for login,
  networking and static-evidence limits. No sandbox is supplied for trusted
  implementation preparation/checks.
- **Isolation limits:** worktrees share Git metadata. Workspace-write restricts
  writes, not all local reads, and the host model connection still uses network.
  Use a dedicated OS user, container or VM for hostile content/sensitive hosts.
  Neither worktrees nor prompt instructions provide complete confidentiality or
  enforcement against malicious trusted tooling.
- **Trusted commands:** setup/checks run outside Codex's sandbox. Reviewed lockfile
  installation, npm lifecycle scripts and repository programs can execute arbitrary
  code, read host files and access network. Project owners must review commands and
  provide their own OS/network controls. No network-dependent installation is
  selected automatically from task text.
- **Credentials and production:** do not copy a source checkout's credentials or
  inherit production secrets. No arbitrary `.env` loading, credential dump or auth
  copy is performed by the orchestrator. Tracked baseline files are still checked
  out normally, including any secrets mistakenly committed there. Configured
  programs may load their own files. Use test-only credentials/services and enforce
  production access restrictions externally; no database safety classifier exists.
- **Local data:** keep configuration outside version control, restrict its file
  permissions, and keep real prompts under ignored root `tasks/` or another private
  directory. Root `results/`, `.env`, `.env.*` and named local config files are
  ignored in this repository. Synthetic material belongs under `examples/`. A
  custom results root elsewhere needs its own storage/version-control protections.
- **Logs:** private run/export directories use owner-only permissions and structured
  files use mode 0600. Raw events, final responses and external tool logs can still
  contain secrets or confidential code. There is no comprehensive automatic
  redaction, rotation or quota. Review before sharing; never put secrets in argv
  or prompts. Metadata does not store configured env values, but tool output may.
- **Human control:** no automatic approval, merge, deployment or result upload occurs.
  Commit/push/draft-PR operations require the separate `publish --draft --confirm`
  command. Trusted commands and manual changes require separate review.

### Cleanup

The following rules describe implementation worktrees. Investigation cleanup
removes only its canonical sealed snapshot after confirmation; changed/unknown
integrity requires `--force`. It retains all other evidence and never touches the
source checkout.

```bash
uv run dev-agent cleanup RUN-ID
# Explicit non-interactive confirmation:
uv run dev-agent cleanup RUN-ID --yes
```

At a terminal, type the exact run ID to confirm. Non-interactive cleanup requires
`--yes`. Worktree identity/configuration must still match; active locks and changed
registrations are refused. Dirty checks include staged, unstaged, untracked **and
ignored** files. A prepared `.venv` or `node_modules` can therefore prevent normal
cleanup even after all source changes have been preserved.

**Destructive opt-in:** only after reviewing and backing up all worktree contents,
you can explicitly discard uncommitted/untracked/ignored work:

```bash
uv run dev-agent cleanup RUN-ID --force --yes
```

Force does not bypass active-run or identity checks, and Git can still refuse
removal. Successful cleanup removes the worktree, retains its branch, canonical
records and exported results, and records cleanup. It never deletes a branch or
prunes unrelated worktrees, and it does not stop containers/test services. Do not
modify a worktree concurrently with cleanup; advisory locks cover this CLI, not
unrelated human or tool activity.

## First execution

This walkthrough uses the source installation and the synthetic `example` project.
Substitute your actual repository path and reviewed checks. No command below should
point to production services.

1. **Select the orchestrator checkout and configure the project.**

   ```bash
   cd /path/to/dev-agent
   config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/dev-agent"
   mkdir -p "$config_dir"
   cp -n examples/config.toml "$config_dir/config.toml"
   ```

   `cp -n` preserves an existing configuration. Edit the file using the complete
   synthetic configuration as a reference. Set repository/branch paths and trusted
   setup/check commands; remove unused synthetic projects. Check test-only service
   requirements. Configuration is not copied into the managed repository.

2. **Confirm the project is configured.**

   ```bash
   uv run dev-agent projects list
   ```

   A successful list does not establish that preparation or model access will work.

3. **Write a human-approved task.**

   ```bash
   mkdir -p tasks
   (
   set -C
   cat > tasks/TASK-123.md <<'TASK'
   # TASK-123: Add a greeting function

   In the existing Python package, add greet(name: str) -> str.

   Acceptance criteria:
   - greet("Ada") returns "Hello, Ada!".
   - Trim surrounding whitespace from the name.
   - Reject an empty or whitespace-only name with ValueError.
   - Add unit tests for these cases using the existing test layout.
   - Follow the mandatory baseline AGENTS.md hierarchy for each changed path and inspect relevant package/test docs.
   - Keep unrelated behaviour unchanged.
   TASK
   )
   ```

   The subshell refuses to overwrite an existing task file. Choose a new identifier
   and private task file if needed. Prompts must be
   nonempty UTF-8 `.md` files, at most 256 KiB, without NUL. Keep acceptance criteria
   and architectural context explicit. The tracked
   [synthetic task example](../examples/tasks/TASK-123.md) is also available.

4. **Inspect a dry-run plan.**

   ```bash
   uv run dev-agent implement TASK-123 --project example \
     --prompt-file tasks/TASK-123.md --verification targeted --dry-run
   ```

   This checks configuration, repository/baseline, prompt and destinations, but
   creates no directories, branches, worktrees, records or exports and invokes no
   Codex or setup/check process. The printed run ID is not reserved. Dry-run does
   not prove authentication, runtime prerequisites or tests will succeed.

5. **Execute the approved task.**

   ```bash
   uv run dev-agent implement TASK-123 --project example \
     --prompt-file tasks/TASK-123.md --verification targeted
   ```

   Setup may download dependencies because you explicitly configured it. Read the
   printed outcome and exit code, including warnings and export errors. Keep the
   actual run ID from this execution, not the dry-run preview.

6. **Inspect the evidence.**

   ```bash
   RUN_ID='REPLACE_WITH_ACTUAL_RUN_ID'
   uv run dev-agent runs show "$RUN_ID"
   uv run dev-agent inspect "$RUN_ID"
   uv run dev-agent results "$RUN_ID"
   ```

   Open `summary.md`, `report.json`, agent output and relevant stdout/stderr logs
   in the printed results directory yourself. If exports failed or are absent,
   use canonical records/logs instead.

7. **Run targeted verification independently.**

   ```bash
   uv run dev-agent verify "$RUN_ID" --profile targeted
   ```

   This appends evidence for the current state without rerunning Codex. If the
   environment needs the reviewed setup again, explicitly opt into
   `verify "$RUN_ID" --profile targeted --prepare`. Use `--profile full` for the
   configured broader suite when its test-only prerequisites are available.

8. **Review the retained worktree.**

   Read its diff, tests, acceptance criteria and instructions using the worktree
   path shown by `inspect`. Confirm which source/profile was verified. Passing
   checks do not approve the implementation, and dev-agent performs no independent
   code review or PR workflow. Preserve valuable changes through your own reviewed
   development process before considering cleanup.

9. **Clean up only when deliberately ready.**

   ```bash
   uv run dev-agent cleanup "$RUN_ID"
   ```

   Confirm the exact run ID. Expect refusal if implementation changes or ignored
   environment files remain. Keep the worktree until the work has been preserved;
   the [separate force operation](#cleanup) permanently discards local files.
   There is no automatic cleanup after execution.

For an installed uv tool, replace `uv run dev-agent` with `dev-agent`. Configuration
paths are stable across invocation directories; a relative `--prompt-file` still
refers to the invoking directory. Global `--config` must precede the subcommand.

## Troubleshooting

Start with `runs show RUN-ID`, `inspect RUN-ID` when the worktree is valid, and the
canonical record/logs. Check preparation, prerequisites, agent status and the
specific verification attempt instead of treating one final label as a diagnosis.
Validation and Codex preflight failures can stop before a run record exists.

| Symptom | Check and response |
| --- | --- |
| Unknown project | Run `projects list`; verify the `projects.NAME` key and selected global `--config` file. There is no task-service lookup or automatic project discovery. |
| Missing prompt file | Confirm the invocation directory or use an absolute path; require an existing nonempty UTF-8 `.md` file within the size limit. |
| Missing baseline branch | Check `refs/heads/NAME` in the configured source. A remote-tracking branch, tag or SHA is not a `base_branch` override. Have the owner prepare the intended local baseline; dev-agent does not fetch it. |
| Existing branch/worktree/results collision | Inspect existing refs, registrations and paths. Nothing is silently overwritten. Preserve the earlier run or use explicit cleanup when appropriate; do not delete work to force creation. |
| Missing Python dependencies or `ModuleNotFoundError` | Inspect preparation logs and the managed project's required Python/install/import paths. Its worktree needs its own dependencies; declare required setup and, where appropriate, explicit `PYTHONPATH = "{worktree}/src"`. pytest import settings do not necessarily propagate to subprocesses. |
| Missing frontend dependencies or unresolved workspace package | Review npm lockfile location, setup `cwd`, installation and shared-package builds. Node/npm/Vitest are not supplied by dev-agent. Add reviewed setup and path/tool prerequisites; do not assume source `node_modules` is available. |
| Missing environment variables | Check `prerequisites`, command `required_env`, explicit env and `inherit_env`. Project/command values apply only to setup/checks. Source `.env`, shell activation and child exports are not automatically inherited. Use disposable test settings, never production credentials. |
| Instruction/scope failure | Inspect `instruction_compliance`, `changed_file_validation`, approved scopes and supplied hashes. Out-of-scope/ignored-file edits, new or altered AGENTS.md, setup replacing required guidance, and audit limits can fail execution. Preserve the worktree; no automatic repair occurs. |
| Verification failed | Read the named attempt's stdout/stderr, exit code and `failure_kind`. Ordinary nonzero results remain generic failures unless explicit prerequisite evidence exists. Investigate setup, baseline behaviour and implementation separately; no baseline control run is performed automatically. |
| Codex authentication/model failure | Preflight checks interface only. Run `codex login` as the same user/`CODEX_HOME`, review agent stderr/events and check the configured model/effort. dev-agent ignores user Codex config and does not forward API-key env variables. Do not relax sandbox policy to bypass an auth error. |
| Agent timeout | Check `agent.timed_out`, duration, partial events and the retained worktree. Adjust the explicit agent timeout only when justified. A later `verify` checks existing work; another `implement` starts a new task execution. |
| Check timeout | Inspect that command's `timed_out`/`failure_kind` and output. Per-command timeout is separate from the agent timeout; no retry or repair follows. |
| Inaccessible/invalid worktree | Check original paths, permissions, branch, common Git metadata and registration. Missing, manually moved, detached or replaced worktrees are refused. Restore/review manually; `runs show` can still expose stored evidence. |
| Cleanup refused | Confirm the run is inactive, identity matches and all uncommitted/untracked/ignored data has been preserved. Git locks/submodules can still block removal. Force is a destructive separate choice, not an automatic fix. |
| No results directory or stale export | Old runs may have no export; new exports can fail independently of canonical persistence. Inspect `results_error` and canonical evidence. Restore original results settings instead of silently relocating history. |
| Wrong virtual environment/read-only cache | Run uv from the dev-agent checkout without selecting an unrelated active environment. For project setup/checks, configure a writable `UV_CACHE_DIR` explicitly if needed; it is not inherited by default. Agent cache restrictions are separate and are not automatically relaxed. |

### Investigation troubleshooting

| Symptom | Action |
| --- | --- |
| Missing Atlassian table | Add `[projects.NAME.atlassian]` to the selected external config; tokens are not TOML settings. |
| HTTP 401/403 | Check named credential variables, expiry, scoped-token `cloud_id`, product token, account project/space visibility and endpoint read scopes. Existing ChatGPT MCP login does not authenticate this CLI. |
| Issue not found | Check uppercase key and configured site; 404 may also mean the issue is not visible. No Jira changes occur. |
| Confluence omission | Check page visibility, read scopes, numeric page links and configured bounds. Search is issue-key-based and capped; warnings constrain suitability. No silent content substitution occurs. |
| Required context unavailable | Check the exact committed baseline, path/case, regular-file status, UTF-8 and byte limits. Dirty checkout copies do not supply context. |
| Bubblewrap/native CLI unavailable | Install `bubblewrap` on a suitable Linux host; qualify user namespaces and a native compatible Codex binary with file-based login. Do not relax the sandbox to bypass a failed probe. |
| Codex authentication/timeout | Read safe agent stderr/events and recorded timeout/exit status; authenticate outside dev-agent and start a new investigation when ready. No automatic retries occur. |
| Missing/invalid AGENTS.md | Maintain a valid root and discovered nested instructions in the selected baseline. Dirty files or another branch do not satisfy this gate. Missing required guidance stops before HTTP/Codex; no optional switch exists. |
| Invalid structured output | Inspect raw `agent-output.md` and the validation error. No accepted `investigation.json` is generated. A zero process exit does not override validation. |
| Read-only violation | `security_failed` rejects the assessment. Examine source/snapshot fingerprints and any concurrent edits; preserve evidence. There is no rollback or autonomous repair. |
| Missing retained snapshot | Inspection still shows canonical evidence; cleanup may have removed the snapshot. `verify` cannot recreate or execute investigation source. |

### Status and exit-code reference

Active phases include `planned`, `creating_worktree`, `preparing`, `implementing`, `validating_instructions`
and `verifying`. Terminal records normally use phase `finished` plus a scoped
outcome. Investigation adds `retrieving`, `loading_context`, `agent_preflight`,
`investigating`, `materializing_snapshot` and `validating` phases, then a terminal `finished` phase.
The original implementation outcome and later verification attempt outcomes remain separate.

| Exit | Meaning |
| --- | --- |
| `0` | Passing selected checks, a structurally accepted investigation (`investigated`), or successful read/dry-run/cleanup. Not semantic proof or human approval. |
| `2` | CLI/configuration/repository/preflight or other command errors returned by the CLI. |
| `3` | Agent execution failed; checks may still have run. |
| `4` | Verification failed, was unconfigured, or otherwise passing work has optional preparation warnings. |
| `5` | Recorded orchestration/export failure or an unrecognised terminal execution outcome. |
| `6` | Required environment preparation/prerequisites failed, or investigation context/namespace preflight failed. |
| `7` | Required Jira retrieval/authentication failed (`retrieval_failed`). |
| `8` | Investigation structured output failed validation (`invalid_investigation`). |
| `9` | Source/snapshot integrity changed or could not be established (`security_failed`). |
| `10` | Implementation instruction/audit/scope gate failed (`instructions_failed`); worktree retained, no accepted implementation. |
| `11` | Confirmed publication failed after its gates; inspect publication phase and recovery evidence. |
| `12` | Publication completed or CI was inspected, but CI is not confirmed passed (including absent/skipped checks and monitoring timeout). |
| `13` | Unified delivery is blocked/incomplete, awaiting confirmation or has CI that is not passed. Adoption is blocked by verification or review. |
| `130` | Interrupted execution. |

`environment_failed` describes required setup or declared prerequisite failures.
`agent_failed` describes unsuccessful agent execution, not an automatically diagnosed
code defect. `verification_failed` describes failed checks or source-changing
verification; command records distinguish `command_failed`, `timeout` and missing
launch/prerequisite evidence. Missing executables discovered while launching a
check may appear as a failed check with `failure_kind = "missing_prerequisite"`
and exit 4, rather than a pre-execution environment gate with exit 6.

`passed_with_warnings` retains optional setup failures. `unverified`, `not_run` and
`skipped` must not be interpreted as passing checks. Historical `succeeded` run
outcomes remain readable. Raw subprocess exit codes, including negative signal
statuses, are recorded separately from the orchestrator's CLI exit code.

## Current limitations

Verified against the current CLI/modules; these are not available capabilities:

- GitHub/Bitbucket task providers, Atlassian Data Center, OAuth browser/3LO flows,
  ChatGPT MCP session reuse, task selection, scheduling or hosted orchestration.
  Jira Cloud issue retrieval and bounded Confluence Cloud retrieval are implemented.
- Comprehensive automatic architectural discovery, execution of tests during
  investigation or feature-branch comparisons. Delivery has an independent candidate
  review stage, but live delivery is blocked on MCP intake qualification.
  Investigation supplies explicit baseline documents; normal Codex project-document
  discovery remains disabled. dev-agent's own baseline hierarchy discovery is mandatory.
- Automatic repair/retry loops, autonomous publication, automatic merging,
  release publication or deployment orchestration. Human-confirmed draft GitHub PR
  publication and commit-specific CI observation are supported separately.
- Multiple agent providers or multiple agents within an execution. Only Codex is
  implemented, using its CLI rather than a direct OpenAI API/SDK integration.
- Strong local-read isolation for implementation, or a sandbox for trusted setup/check commands;
  production-DSN detection, general secret-log redaction, automatic test-service provisioning
  or teardown, and complete dependency/environment fingerprinting.
- Arbitrary commit/tag/remote baseline selection, automatic fetch, submodule setup,
  transparent checkout-filter/LFS handling, or verified Windows/macOS support.
- Automatic baseline regression comparison or reconstruction of old source states
  without fingerprints; parsing every test framework's internal skips/coverage;
  automatic migration of historical runs or exported results.
- Guaranteed terminal reports after SIGKILL/power/disk failure, export-directory
  transactions, log rotation/quotas or protection against unrelated concurrent edits.

Broader task-provider support, runtime investigation verification or independent
review would require explicit implementation and validation. They
should not be inferred from Codex's general capabilities or a successful subprocess.

## Package architecture

The [developer architecture guide](architecture.md) explains package ownership,
control flow and how to run focused tests.
