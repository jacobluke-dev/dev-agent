# Quick Start and CLI reference

Use Dev Agent to prepare a reviewed task, implement it in a retained Git worktree,
run project checks and publish an approved draft PR. **Live Jira-to-PR
`deliver` is currently blocked at Atlassian MCP intake.** You can inspect the
delivery plan and its blocker today. For a working implementation path, supply a
reviewed Markdown brief to `implement`, then use the steps below.

- [Install](#install)
- [Configure a project](#configure-a-project)
- [Jira MCP and OAuth](#jira-mcp-and-oauth)
- [Walk through delivery](#walk-through-delivery)
- [Adopt a retained legacy implementation](#adopt-a-retained-legacy-implementation)
- [Command reference](#command-reference)
- [Flag reference](#flag-reference)
- [Recovery and resumption](#recovery-and-resumption)
- [Troubleshooting](#troubleshooting)
- [Reading the output](#reading-the-output)

## Prerequisites

| Tool or access | Required for |
| --- | --- |
| Ubuntu/Linux and Python 3.14 | The supported Dev Agent environment. The checkout selects 3.14 and package metadata requires `>=3.14`. |
| uv | Installation and the commands in this guide. No minimum uv release is pinned. Use a version that supports `uv sync --locked`, Python 3.14 and editable tool installation. The guide was checked with uv 0.11.11. [Install uv](https://docs.astral.sh/uv/getting-started/installation/). |
| Git | A local clone of the managed repository, a local base branch and worktree support. Commit the root `AGENTS.md`, nested instructions and configured required architecture documents to the baseline first. Commit identity must be configured before publication. |
| Codex CLI and existing model login | Live implementation, investigation and review. The tested CLI interface is 0.169.0. Plain configuration, record inspection and verification do not need a Codex account. Codex is the only supported runner. |
| Native Codex binary, file-backed Codex login and Bubblewrap | Read-only investigation and independent review. The runner requires Linux mount/PID namespace support and a readable `auth.json` in the configured Codex home. |
| GitHub CLI (`gh`) and GitHub.com access | Publication preview, confirmed draft publication and CI observation. Use an account with the required repository access, including push permission for preflight. |
| Managed-project tooling | Preparation and checks run with the managed project's toolchain. The example below assumes a Python service with a uv lockfile, pytest and Ruff. Install any additional runtimes or test services that your own project requires. |
| Jira/Confluence access | The standalone `investigate` command uses the supported read-only REST configuration. Interactive MCP qualification uses OAuth, separately from Codex model sign-in. The MCP integration does not currently enable live delivery. |

## Install

Clone using your authorised repository URL. The commands below use the checkout's
GitHub origin in HTTPS form. If access is restricted, obtain access through your
usual Git credentials or use the SSH origin.

```bash
git clone https://github.com/jacobluke-dev/dev-agent.git
cd dev-agent
uv python install 3.14
uv sync --locked
uv run python --version
uv run dev-agent --help
```

Install the native CLI via the [Codex installation instructions](https://developers.openai.com/codex/cli/),
then authenticate as the user who runs Dev Agent. Ubuntu users can install
`bubblewrap` and `gh` through their package manager where available. Bubblewrap
is needed for sealed investigation and review. The `gh` steps are needed for
publication and CI.

```bash
codex --version
codex exec --help
codex login
gh --version
gh auth login --hostname github.com
gh auth status --hostname github.com
```

Authentication takes place outside Dev Agent. Keep credentials out of TOML,
prompts and command arguments. For other package sources see the
[GitHub CLI installation instructions](https://github.com/cli/cli#installation).

To use Dev Agent from other directories, install an editable tool from the checkout:

```bash
uv tool install --python 3.14 --editable .
export PATH="$(uv tool dir --bin):$PATH"
dev-agent --help
```

Keep the checkout available for an editable installation. A wheel install should
set `results_root` explicitly because the default is relative to the installed
package. For additional options see [installation](setup.md#installation).

## Configure a project

The default file is `${XDG_CONFIG_HOME:-$HOME/.config}/dev-agent/config.toml`.
An explicit global `--config PATH`, placed **before** the subcommand, selects
another whole file. Files are not merged. Relative paths are based on the
selected TOML's directory. Existing settings should be edited in place instead of
being duplicated in a second project table.

```bash
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/dev-agent"
mkdir -p "$config_dir"
cp -n examples/config.toml "$config_dir/config.toml"
```

The following is a minimal configuration for a fictional Python service. Adapt
the copied file to your repository and remove any sample project tables you do
not use. Set `repository` to your clone, `base_branch` to an existing local
branch and `github.repository` to the matching GitHub.com `owner/repository`.
Confirm that the committed baseline contains `AGENTS.md` and the paths used by
your checks. Update or clear any sample architecture paths in the copied file.

```toml
runs_root = "~/.local/state/dev-agent/runs"
results_root = "~/.local/state/dev-agent/results"

[projects.example]
repository = "~/src/example-service"
base_branch = "main"
worktree_root = "~/.local/state/dev-agent/worktrees/example"

[projects.example.agent]
# Omit model to use the Codex CLI default, or specify an available model ID.
reasoning_effort = "medium"
timeout_seconds = 1800

[[projects.example.preparation]]
name = "python-dependencies"
argv = ["uv", "sync", "--locked"]
cwd = "."
timeout_seconds = 600

[projects.example.preparation_cache]
inputs = ["pyproject.toml", "uv.lock"]
outputs = [".venv/pyvenv.cfg", ".venv/bin/pytest"]

[[projects.example.verification_profiles.targeted]]
name = "unit-tests"
argv = ["uv", "run", "pytest", "tests/unit"]
timeout_seconds = 300
required_paths = ["tests/unit"]

[[projects.example.verification_profiles.full]]
name = "regression"
argv = ["uv", "run", "pytest"]
cwd = "."
timeout_seconds = 900

[[projects.example.verification_profiles.full]]
name = "lint"
argv = ["uv", "run", "ruff", "check", "."]
timeout_seconds = 120

[projects.example.github]
repository = "example-org/example-service"
allowed_paths = ["src", "tests"]
ci_timeout_seconds = 600
poll_interval_seconds = 10
```

`example` is the identifier passed to `--project`. Keep canonical records,
worktrees and private results outside the managed checkout. In this example,
targeted runs the unit tests and full runs the entire test suite plus lint.
Checks start at the retained worktree root unless their `cwd` selects a directory
within it. Use the managed project's actual verification commands and declare
their prerequisites. If the project needs a database, configure an isolated
test instance and keep credentials out of the TOML and worktree.

Configure all setup commands and test prerequisites in external TOML. Dev Agent
runs the configured setup before implementation and before pending checks in
delivery or adoption. Commands may contact registries or execute project package
scripts. Review the repository and lockfiles before enabling them. The optional
cache uses recorded success, file hashes and installed output files to avoid
another install. Adapt those paths to your toolchain. Standalone `verify` uses
the existing environment by default. `verify --prepare` explicitly forces setup.

Agent, setup, verification and CI monitoring each have their own timeout.
`agent.model` and `agent.reasoning_effort` are passed to Codex when supplied.
There is no separate model profile flag. `implement --verification` and
`verify --profile` select checks. Both use targeted when configured, otherwise
legacy, unless you select a profile explicitly. `deliver` requires targeted
and full. See [configuration](setup.md#project-configuration) and
[verification](setup.md#verification) for all fields and bounds.

```bash
uv run dev-agent projects list
uv run dev-agent deliver TASK-123 --project example --draft --dry-run
```

The dry run validates project, repository, instructions, profile and publication
settings. With valid local inputs, current code reports `mcp_unavailable` and
exits 13. Runtime prerequisites, model access and GitHub authentication are
checked separately when needed.

## Jira MCP and OAuth

MCP qualification uses a native Atlassian **Streamable HTTP** server over HTTPS
and Codex MCP OAuth. This is **host-side setup for qualification**, not a switch
that enables Dev Agent delivery. There is no project MCP table or alternative
credential supported for `deliver`.

1. Ask your Atlassian administrator to approve a client and site with read access
   to the intended Jira issue and supporting Confluence pages. Exclude write and
   generic dispatch tools. Enforce read-only access in the Atlassian server and
   confirm the tool allowlist. Prompting the model to avoid writes cannot establish
   that boundary. See [Atlassian MCP permissions](https://support.atlassian.com/security-and-access-policies/docs/configure-atlassian-mcp-server-permissions/).
2. If the approved connection is absent, register it in Codex with the
   organisation-approved Streamable HTTP URL. The historical probe used
   `https://mcp.atlassian.com/v2/mcp`. Confirm the approved OAuth endpoint for
   your account with the organisation before setup. Complete browser OAuth in
   Codex and review the consent. See [Codex MCP configuration](https://developers.openai.com/codex/mcp/).

   ```bash
   read -r -p "Approved Atlassian MCP URL: " atlassian_mcp_url
   codex mcp add atlassian --url "$atlassian_mcp_url"
   codex mcp login atlassian
   codex mcp list
   ```

3. A configured server name proves registration only. From an approved
   interactive Codex session outside managed repositories, check the server's
   tools with `/mcp`. Once transport, authentication and read-only tooling are
   confirmed, ask for a bounded read of one issue you are authorised to access
   by exact key. Inspect the returned key, description and acceptance criteria
   against Jira. Keep the response and credentials private. This checks the
   interactive connection only.
4. Dev Agent qualification separately tests the isolated runtime's transport,
   scoped OAuth reuse, schema and tool filtering, and ability to preserve source
   provenance. The isolated catalogue reached the service with network access
   but reported `notLoggedIn`. Host login or a successful interactive issue
   read does not qualify this boundary. Follow the safe diagnostics in
   [MCP qualification](mcp-qualification.md).

`mcp_unavailable` stops live delivery before implementation or publication.
Keep the sandbox and credential boundary in place. For a separate advisory
workflow, see the [standalone investigation setup](setup.md#jira-investigation).

## Walk through delivery

The intended one-command sequence uses the fictional Jira issue `TASK-123`.
For your own project, use a ticket key you are authorised to access.

```bash
uv run dev-agent deliver TASK-123 \
  --project example \
  --draft \
  --confirm \
  --watch
```

Today, use the dry run first. Live `deliver` records the intake blocker even
with `--confirm`. The stages after intake are implemented and tested with
synthetic ticket evidence. A real authenticated Jira-to-PR run has not passed
qualification.

| Stage | What the controller does |
| --- | --- |
| Configuration and instructions | Load external settings, resolve the exact local base commit, read committed `AGENTS.md` hierarchy and mandatory context. This happens before intake. |
| Jira MCP | Require isolated, authenticated and bounded source retrieval. Current live delivery stops here. |
| Investigation and brief | Validate the snapshot and baseline assessment, preserve original requirements and citations, and assemble the implementation brief. Uncertain suitability can require a human decision. |
| Model and worktree | Select the configured Codex model or CLI default. Prepare an isolated task worktree at the recorded commit and run the implementation with its applicable instructions. |
| Verification and independent review | Require passing targeted and full evidence for the same source. A separate read-only runner reviews the requirements and candidate with source citations. Missing or blocked review prevents publication in delivery. |
| Draft publication | Preview approved files and verify source, destination and instruction evidence. `--confirm` authorises the validated commit, non-force push of the task branch and, if needed, draft creation. It cannot bypass gates. |
| CI and final report | Observe check runs for the exact commit, polling with `--watch`. Record status, execution ID, commit and draft URL when available. The operator merges separately. |

See [architecture](architecture.md#delivery-lifecycle) for details and diagrams.

### Use the standalone workflow today

1. Prepare a reviewed task file with the requirements, acceptance criteria,
   relevant source references and approved scope. Start with the
   [sample task](../examples/tasks/TASK-123.md) and adapt it to your repository.
   You can use Jira manually or an advisory `investigate` report as input.

   ```bash
   mkdir -p tasks
   cp -n examples/tasks/TASK-123.md tasks/TASK-123.md
   ```

   After reviewing the brief, preview the implementation before running it.

   ```bash
   uv run dev-agent implement TASK-123 --project example \
     --prompt-file tasks/TASK-123.md --verification targeted --dry-run
   uv run dev-agent implement TASK-123 --project example \
     --prompt-file tasks/TASK-123.md --verification targeted
   ```

2. Copy the new implementation run ID from the output. Inspect the worktree and
   run full verification. `RUN-ID` in the examples is a placeholder.

   ```bash
   uv run dev-agent inspect RUN-ID
   uv run dev-agent results RUN-ID
   uv run dev-agent verify RUN-ID --profile full
   ```

   Perform independent human review of standalone runs. There is no `review`
   subcommand. Automated independent static review is a stage of `deliver`,
   gated by its validated source evidence. Inspect the linked implementation
   record and retained review files if this stage ran.

3. If approved, check the publication preview and verify the file manifest,
   destination, check evidence and workflow triggers. A publication preview
   performs GitHub reads. It makes no local Git changes or remote writes.

   ```bash
   uv run dev-agent publish RUN-ID --draft --dry-run
   uv run dev-agent publish RUN-ID --draft --confirm --plan-sha256 REVIEWED-PLAN-SHA256
   uv run dev-agent ci RUN-ID --watch
   ```

   Replace `REVIEWED-PLAN-SHA256` with the digest printed by preview. Pass the
   same preview options on confirmation. If evidence or remote state changes,
   review the new plan. `--confirm` does not authorise merging, deployment
   or skipping mandatory review or checks. The push or draft PR can trigger
   existing GitHub workflows, including owner-configured deployment jobs.
   Review the workflow policy before confirming. Check the draft and CI before
   manually merging.

## Adopt a retained legacy implementation

Use `adopt` for a completed, unpublished implementation whose record lacks
historical instruction-enforcement evidence. Dev Agent reuses its exact Git
worktree and checks its baseline, current source, applicable `AGENTS.md` files,
configured modification scope and allowed publication paths. It runs targeted
and full verification and an independent read-only review. The original run
record stays intact. The new adoption record owns the current evidence.

From the directory containing the original `tasks/TASK-123.md`, run:

```bash
uv run dev-agent adopt ORIGINAL-RUN-ID
```

Dev Agent uses the brief only when its hash matches the original run. If that
file is unavailable, point to a successful investigation of the same Jira
ticket and project with `--requirements-run INVESTIGATION-RUN-ID`. This reuses
the recorded, integrity-checked snapshot and requires no task file. If neither
source is available, adoption stops. Adoption runs configured setup automatically
before pending verification. Add `--prepare` to force setup for the first
pending check. The audit hashes Git-visible source and inspects
ignored paths for scope and instruction changes. Ignored dependency contents
do not consume the adoption byte budget. Eligible content and inspected path
counts must fit the configured audit limits.

Inspect the new adoption ID and the exact source, verification and review
evidence. Historical enforcement remains `not_recorded` even after the current
checks pass. To accept that limitation for this exact candidate, supply the
printed `approval_sha256`. That records your decision locally. Publication
still requires a separate preview and confirmation.

```bash
uv run dev-agent inspect ADOPTION-RUN-ID
uv run dev-agent results ADOPTION-RUN-ID
uv run dev-agent adopt ORIGINAL-RUN-ID --accept-legacy APPROVAL-SHA256
uv run dev-agent publish ADOPTION-RUN-ID --draft --dry-run
uv run dev-agent publish ADOPTION-RUN-ID --draft --confirm --plan-sha256 REVIEWED-PLAN-SHA256
uv run dev-agent ci ADOPTION-RUN-ID --watch
```

The adoption route requires passing full verification and review without a
waiver. It cannot adopt a run whose original instruction audit failed. Before
publication starts, rerun `adopt ORIGINAL-RUN-ID` to resume unfinished checks.
Changes to the source, configuration or requirements require new evidence and
approval. Once publication starts, use `publish` with the same adoption ID to
recover its recorded commit, push and draft PR. The original ID remains the
worktree owner for explicit cleanup.

## Command reference

Run these examples from the Dev Agent checkout with `uv run`. With a tool
installation, use `dev-agent` directly. `TASK` is the ticket or task identifier.
`RUN-ID` identifies a canonical run, and `EXECUTION-ID` specifically identifies
the parent delivery run. Use its linked `implementation_run_id` for `verify`,
`publish`, `ci` and worktree cleanup.

| Command and example | Inputs and options | Default behaviour and output | Common refusals or incomplete outcomes |
| --- | --- | --- | --- |
| `dev-agent projects list` | Required `list` action | Lists configured names, repository paths, base branches and legacy check names as JSON. | Invalid configuration. This listing does not establish that named profiles will pass. |
| `dev-agent implement TASK-123 --project example --prompt-file tasks/TASK-123.md` | `TASK`, `--project`, `--prompt-file`. Optional `--verification`, `--scope`, `--dry-run`. | Creates a retained worktree, prepares it, invokes Codex and verifies with targeted or legacy. Prints the run record and paths. Dry-run prints a local plan. | Invalid task or instruction context, unavailable runner, failed setup, agent or checks. A retained run can be unverified. |
| `dev-agent adopt ORIGINAL-RUN-ID` | Original retained implementation ID. Optional `--requirements-run`, `--prepare` or separate `--accept-legacy`. | Audits the current candidate, qualifies both verification profiles and obtains read-only review. A separate acceptance invocation records legacy provenance approval. Prints the linked adoption ID and evidence status. | Ineligible original run, missing requirements, invalid candidate or scope, failed checks or review, stale approval. |
| `dev-agent investigate TASK-123 --project example --dry-run` | Jira issue key, `--project`. Optional `--dry-run`. | Live reads preconfigured Atlassian REST sources, runs read-only Codex against a sealed baseline, and records an advisory report. Dry-run prints a local plan. | Missing provider config or credentials, failed retrieval, unavailable mandatory context, invalid assessment or failed snapshot integrity. |
| `dev-agent deliver TASK-123 --project example --draft --dry-run` | Jira issue key, `--project`, `--draft`. Optional `--dry-run`, `--confirm`, `--watch`, `--resume`. | Dry-run prints local preflight. Live records a delivery and currently stops at MCP intake. With qualified intake, it can run through publication preview, and confirmed runs can publish a draft. CI takes one observation by default. | Missing profiles or instructions, `mcp_unavailable`, incomplete checks or review, publication blockers, incompatible checkpoint, or CI without a pass. |
| `dev-agent verify RUN-ID --profile full` | Implementation ID. Optional `--profile`, `--prepare`. | Runs targeted or legacy by default in the retained worktree. Appends a source-bound attempt and prints its JSON and the original outcome. | Invalid or removed worktree, missing profile, failed prerequisites or failed checks. |
| `dev-agent publish RUN-ID --draft --dry-run` | Implementation ID and `--draft`. Optional `--dry-run` or `--confirm`, `--file`, `--title`, `--summary`, `--plan-sha256`, `--allow-incomplete-full`, `--full-verification-reason`, `--acknowledge-stale-base`. | Previews by default. Uses authenticated GitHub reads and prints the plan, digest and blockers. Confirmation permits commits, pushes and draft creation. A published result includes the commit, PR and CI observations. The command polls CI on confirmed publication by default. | Changed source or evidence, unapproved files, invalid destination, missing review for delivery-linked runs, missing authentication, stale base, partial publication or CI without a pass. |
| `dev-agent ci RUN-ID --watch` | Published implementation ID. Optional `--watch`. | Reads once by default. Appends observations for the recorded commit and prints check-state JSON. | Missing publication, remote PR head mismatch, failed, skipped or absent checks, timeout or GitHub read errors. |
| `dev-agent runs list` | Required `list` action | Lists canonical runs as JSON, including kind, outcome and latest verification outcome when present. | Invalid storage/configuration or unreadable records. |
| `dev-agent runs show RUN-ID` | Required `show` action and known run ID | Prints canonical JSON for an implementation, investigation or delivery. | Unknown ID or invalid record. |
| `dev-agent inspect RUN-ID` | Known run ID | Prints inspection JSON with retained paths, relevant current state and workflow evidence. Makes local inspection calls. | Unknown ID, invalid record or inconsistent retained state. |
| `dev-agent results RUN-ID` | Known run ID | Prints the existing exported results directory. | Historical runs may lack an export. Use `runs show` for their canonical evidence. |
| `dev-agent cleanup RUN-ID` | Implementation or investigation ID. Optional `--yes`, `--force`. | Prints inspection and asks a terminal user to type the run ID. Removes the confirmed worktree or sealed investigation snapshot. Keeps run records and the implementation branch. | Missing confirmation, dirty or changed contents, invalid retained identity, concurrent operation or delivery parent ID. |

Independent automated review runs inside `deliver`. Inspect the linked
implementation record for its status and the referenced review artifacts for
findings. There is no standalone `review` command.

## Flag reference

| Flag | Commands | Required, default and effect |
| --- | --- | --- |
| `-h`, `--help` | Root and each command/action | Optional. Prints usage and exits without loading project config. |
| `--config PATH` | Root, before the command | Optional. Selects one external TOML file. Default is the XDG config path shown above. |
| `--project NAME` | `implement`, `investigate`, `deliver` | Required. Selects a configured project. |
| `--prompt-file PATH` | `implement` | Required. Reads the approved Markdown brief. Relative paths are resolved from the command's working directory. |
| `--scope PATH` | `implement` | Optional and repeatable. Selects literal baseline files or directories. An explicit list replaces the configured scope. Globs and paths outside the repository are invalid. |
| `--verification {targeted,full,legacy}` | `implement` | Optional. Defaults to targeted if configured, otherwise legacy. Chooses trusted checks to run after implementation. |
| `--profile {targeted,full,legacy}` | `verify` | Optional. Defaults to targeted if configured, otherwise legacy. Chooses a check profile only. |
| `--prepare` | `verify`, `adopt` | Off by default. Forces configured setup in the retained worktree. Adoption normally ensures setup automatically. The flag forces its first pending check to run setup and is disallowed during legacy acceptance. Trusted commands may install dependencies or use the network. |
| `--requirements-run INVESTIGATION-RUN-ID` | `adopt` | Optional. Uses the saved requirements snapshot from the same project's successful investigation. By default, adoption looks for the exact original brief. |
| `--accept-legacy APPROVAL-SHA256` | `adopt` | Optional. Records operator acceptance of missing historical instruction evidence for the previously qualified candidate. Separate from `--prepare`, `--requirements-run` and publication confirmation. |
| `--draft` | `publish`, `deliver` | Required. Selects the supported PR mode. Requires `--confirm` before publication mutations. |
| `--dry-run` | `implement`, `investigate`, `deliver`, `publish` | Off by default. Previews and does not write state. `publish` previews by default in the absence of `--confirm` and rejects combining the two flags. `deliver` gives the dry-run branch precedence. |
| `--confirm` | `publish`, `deliver` | Off by default. Authorises a validated commit, push and draft PR creation only. It does not authorise merging, deployment or bypassing checks. |
| `--watch` | `ci`, `deliver` | Off by default. Polls GitHub checks for the recorded commit using the configured interval and deadline. A timeout leaves the remote checks running. |
| `--resume EXECUTION-ID` | `deliver` | Optional. Reopens a recorded parent checkpoint with matching project, ticket and configuration evidence. Without it a live call starts a new delivery. Use `--confirm` for outstanding publication writes and `--watch` for polling. |
| `--file PATH` | `publish` | Optional and repeatable. Explicit selection must match the complete eligible change set. If omitted, the controller proposes the eligible set. Review the manifest before confirming. |
| `--title TEXT` | `publish` | Optional. Supplies a reviewed ticket-prefixed title for the commit and PR. Defaults to the ticket plus `Approved implementation`. |
| `--summary TEXT` | `publish` | Optional. Supplies reviewed PR summary text. Default uses the controller's generated summary. |
| `--plan-sha256 HASH` | `publish` | Optional. Requires the rebuilt plan to match the reviewed preview digest. A mismatch blocks confirmation. |
| `--allow-incomplete-full` | `publish` | Off by default. Acknowledges incomplete full verification with a non-empty `--full-verification-reason`. Passing targeted evidence and all other gates still apply. The acknowledgement is recorded and disclosed in the draft. |
| `--full-verification-reason TEXT` | `publish` | Optional unless `--allow-incomplete-full` is set. Explains the incomplete full checks. Pair the two flags. |
| `--acknowledge-stale-base` | `publish` | Off by default. Accepts a reported base-branch advance after review. It does not rebase the worktree or override repository identity checks. |
| `--yes` | `cleanup` | Off by default. Confirms removal without an interactive prompt. Non-interactive removal requires this flag. |
| `--force` | `cleanup` | Off by default. With confirmation, permanently discards dirty, untracked and ignored files or changed snapshot contents. Inspect and preserve anything needed first. |

`implement`, `investigate` and `deliver` dry runs read local configuration and
baseline evidence. They do not launch Codex, run project checks, contact Jira
or GitHub, or write run records or worktrees. A `publish` preview reads GitHub
authentication, repository and PR information to check eligibility. It does
not stage, commit, push or create a PR. `ci` is read-only on GitHub and appends
local observations. For confirmed publication, collect the preview hash and
re-run with the same options as shown in the [walkthrough](#walk-through-delivery).

## Recovery and resumption

1. Find the correct ID and inspect it before issuing another write command.

   ```bash
   uv run dev-agent runs list
   uv run dev-agent runs show EXECUTION-ID
   uv run dev-agent inspect EXECUTION-ID
   uv run dev-agent results EXECUTION-ID
   ```

   Inspect the linked implementation record for verification, review and
   publication evidence once an implementation ID exists.

2. Resume against the **same** ticket, project and operator configuration.
   Dry-run checks local compatibility before reusing the checkpoint. A live
   resume reuses validated evidence and repeats only work required to advance
   the recorded phase. It does not turn a failed or interrupted agent into a
   passing run. The current MCP intake blocker also applies on resume.

   ```bash
   uv run dev-agent deliver TASK-123 --project example --draft \
     --resume EXECUTION-ID --dry-run
   uv run dev-agent deliver TASK-123 --project example --draft \
     --resume EXECUTION-ID --confirm --watch
   ```

3. For failed checks on an intact worktree, inspect the failed check and its
   logs, correct the prerequisite or review the source change, then reverify.
   Omit `--prepare` to check the existing environment. New attempts keep their
   own source fingerprint and leave earlier evidence intact. For publication,
   recheck both profiles on the exact candidate and ensure required review is
   still current.

   ```bash
   uv run dev-agent verify RUN-ID --profile targeted --prepare
   uv run dev-agent verify RUN-ID --profile full
   ```

4. For interrupted publication, inspect `publication` in the implementation
   record, then preview again and compare its digest and manifest. After
   authorising the current plan, repeat confirmed publication for **the same
   run**. The controller reconciles the journalled commit, branch and matching
   draft with the remote before continuing. A recorded matching push or PR is
   reused. A contradictory remote state must be resolved by the operator.
   Once published, use `ci` or `ci --watch` and open failing check URLs to
   investigate the Actions logs. Observation never dispatches or reruns CI.

5. After review and any recovery work, inspect before confirming cleanup.
   Enter the run ID at the prompt. Use `--yes` only after checking the path in
   a non-interactive workflow. Preserve anything uncommitted that you need.

   ```bash
   uv run dev-agent inspect RUN-ID
   uv run dev-agent cleanup RUN-ID
   ```

## Troubleshooting

| Symptom | How to investigate and resolve it | Can it resume? |
| --- | --- | --- |
| `mcp_unavailable` | Inspect blockers and [qualification results](mcp-qualification.md). Confirm host registration, OAuth and bounded read-tool access separately. Isolated authentication and tool/provenance qualification must pass before live delivery can proceed. Use a reviewed brief with standalone `implement` for manual intake. | Resuming today encounters the same intake gate. |
| Missing verification profiles | Check the external TOML selected by `--config`, the project ID and the named profile tables. Configure genuine targeted and full checks, then repeat preflight. Set each profile to the managed repository’s checks and supply any required test services. | Start a new delivery if configuration changed. Standalone checks can be appended to an intact old worktree. |
| Unsupported provider, model or reasoning setting | Check `[projects.NAME.agent]`. Codex is the available runner. Use an account-supported model and a supported reasoning setting, or omit the optional values. See the saved agent error and [agent settings](setup.md#agent-properties). | Start a new implementation for a changed model or config. |
| Missing authentication | Identify which stage failed. Check Codex model login, the file-backed login used for sealed runs, or host Atlassian OAuth as applicable. For standalone Jira retrieval, validate the configured credential variable names using [setup](setup.md#jira-investigation). | Restoring the same required access can unblock a checkpoint. It cannot supply missing source or model execution evidence. |
| Invalid configuration | Run `projects list`, check the printed schema error and compare the selected file with the example. Check relative paths against the TOML directory. | Re-run preflight after correcting it. A changed delivery configuration requires a new execution. |
| Missing or invalid instructions | Inspect the exact baseline and required document paths, hashes and scope audit. Commit missing documents through the repository's normal process. Review any out-of-scope changes. | An eligible run with missing historical evidence can use [adoption](#adopt-a-retained-legacy-implementation). Invalid current context or a failed historical audit blocks adoption. |
| Targeted verification failed | Read the latest attempt and its stdout/stderr. Separate reported prerequisites from failed test assertions. Correct the environment or candidate under review and rerun targeted and full as needed. | Append checks to an intact retained worktree. A new source identity requires current verification and any applicable review evidence. |
| Full verification incomplete | Inspect the full profile logs, prerequisites and fingerprint. Rerun full on the reviewed source. Standalone publication of a normal implementation accepts a separately recorded incomplete-full acknowledgement with a reason. | Reinspect the plan after verification. Delivery and legacy adoption require full verification to pass. |
| Publication blocked | Compare the plan blockers with local scope, staged state, source fingerprints, required review and the intended remote and base branch. Resolve eligible issues and preview again. | Reuse the same run for a compatible partial publication. Conflicting state needs manual reconciliation. |
| GitHub authentication failed | Check `gh auth status --hostname github.com` and access to the configured origin with the intended account. Authenticate via `gh` outside Dev Agent. | Preview again, then reconfirm if required. |
| Missing or skipped CI checks | Inspect the PR head SHA, recorded `ci` observation, `workflow` evidence and the GitHub Actions triggers. Draft conditions can skip workflows. Missing, skipped and unavailable checks do not establish a pass. | Observe again with `ci --watch` if checks are expected to appear. |
| Interrupted run | Inspect the canonical phase, outstanding blockers and any retained worktree. Use `--resume` on a compatible delivery checkpoint. For a partially published implementation, preview before reconfirming. | Depends on the last durable phase. A partial implementation is retained for human review. |
| Stale or incompatible record | Use `runs show` and `inspect` to compare task, project, baseline and recorded evidence with current inputs. A historical record may lack fields required by newer gates. | Keep the old evidence and start a new execution when the gate cannot be proven. |

See [exit statuses](setup.md#status-and-exit-code-reference) for exact codes. A delivery that is
incomplete or blocked exits 13. A completed publication can exit 12 when CI
has not passed. Inspect the recorded PR rather than assuming that exit 12
means no draft exists.

## Reading the output

The following is an illustrative excerpt of a **live blocked delivery**.
Progress goes to stderr and the summary goes to stdout. The sample IDs and
paths in this section are placeholders.

```text
dev-agent delivery: "retrieving_mcp_requirements"
```

```json
{
  "execution_id": "EXECUTION-ID",
  "jira_ticket": "TASK-123",
  "status": "mcp_unavailable",
  "generated_plan": null,
  "verification": [],
  "review": "not_performed",
  "commit_sha": null,
  "draft_pr_url": null,
  "ci_status": "not_performed"
}
```

Check `blockers` for the outstanding reason and `results_directory` for the
export location. Empty verification and absent commit/PR fields mean those
stages did not run. `runs show` retains the authoritative evidence.

The following excerpt illustrates a **synthetic downstream execution** with
passing verification, review and CI. It is not a live qualification result.
For real runs, inspect the linked implementation record's `agent` entry for the
configured model and runner. A null configured model means the CLI default was
requested. The record does not identify the model resolved by that default.

```json
{
  "agent": {
    "provider": "codex",
    "config": {"model": null, "reasoning_effort": "high", "timeout_seconds": 1800}
  }
}
```

Inspect the independent `review.json` artifact for criterion coverage,
findings and their citations. For example,
a passing synthetic review may record `"verdict": "passed"` and
`"findings": []`. Findings, when present, carry a `severity`, an explanation
and repository evidence. The parent summary combines the stage statuses:

```json
{
  "execution_id": "EXECUTION-ID",
  "jira_ticket": "TASK-123",
  "status": "delivered",
  "implementation_run_id": "RUN-ID",
  "generated_plan": "/path/to/canonical/run/implementation-brief.md",
  "verification": [
    {"profile": "targeted", "status": "passed", "outcome": "passed"},
    {"profile": "full", "status": "passed", "outcome": "passed"}
  ],
  "review": "passed",
  "commit_sha": "1111111111111111111111111111111111111111",
  "draft_pr_url": "https://github.com/example-org/example-service/pull/42",
  "ci_status": "passed",
  "blockers": []
}
```

For configuration details, see [Setup and operations](setup.md). For package
responsibilities and the delivery diagrams, see [Architecture](architecture.md).
