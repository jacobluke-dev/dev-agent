# Codex MCP replacement qualification

For current `deliver` preflight and supported alternatives, see the
[Quick Start](quickstart.md#jira-mcp-and-oauth).

The MCP replacement is **not qualified**. `dev-agent investigate` currently uses
its existing Atlassian REST provider; no MCP provider switch has been added.
Implementation remains independent of user MCP configuration. This document records
the migration gate and the minimum work needed to reopen it, rather than describing
an available MCP investigation feature.

The [2026-11-17 diagnostic follow-up](#diagnostic-follow-up-2026-11-17) below
separates the original network failure from a second authentication boundary.
The initial qualification evidence and migration gates remain preserved here.

## Existing components to retain

Project/baseline validation, sealed Git snapshots, the shared mandatory instruction
resolver, read-only execution, source-integrity checks, investigation-v1 assessment
validation and canonical/exported results can all be reused. Requirements retrieval
and source capture are the proposed change. No additional agent framework or direct
OpenAI API integration is needed.

## Installed runtime findings

Qualification inspected Codex CLI **0.169.0** using CLI help, generated protocol
schemas, redacted local registration metadata and authoritative documentation.
Registration is distinct from connectivity, OAuth readiness and tool authorization.
No user configuration or authentication values belong in this public document.

| Question | Finding |
| --- | --- |
| Registration | Native servers are configured under `mcp_servers.NAME` in Codex configuration. Plugins can separately bundle servers. |
| Unattended execution | The runtime documents MCP in `codex exec`; an enabled required server can stop startup when unavailable. Successful unattended Atlassian execution was **not** demonstrated here. |
| Server isolation | Ignoring user configuration plus explicitly supplying a controlled server definition is a possible design. Removing `--ignore-user-config` would load unrelated settings and is not an acceptable shortcut. |
| Tool isolation | Native `enabled_tools` and `disabled_tools` fields are documented. Exact read-tool identities, input schemas and disallowed-call rejection still require qualification against the working server. |
| Approval | `approval_policy="never"` does not itself make remote tools read-only. MCP has separate tool approval settings; their unattended allow/deny behaviour must be tested. |
| OAuth | Codex supports existing MCP OAuth credentials with an `auto`, `file` or `keyring` store preference. Reuse in the isolated execution environment remains unverified. |
| User config | Installed `exec --help` confirms `--ignore-user-config` skips `config.toml` while retaining the authentication home. Registrations skipped by that flag need controlled reconstruction. |
| Rules | Installed help defines `--ignore-rules` as execpolicy rule-file exclusion; it is not an MCP allowlist. |
| Plugins | Disabling plugins does not substitute for restricting native MCP server definitions. The native HTTP registration was attempted with plugins/apps disabled during qualification. |
| Networking | Read-only repository permissions do not make remote MCP side effects impossible. MCP transport and model connectivity need their own controls; broad shell networking must remain disabled. |
| Filesystem | OAuth may require a credential cache or OS keyring outside the repository. No complete home directory, credential store copy or keyring bus should be exposed to the agent to bypass a failed probe. |

References: [Codex MCP configuration](https://developers.openai.com/codex/mcp/),
[configuration reference](https://developers.openai.com/codex/config-reference/),
[non-interactive execution](https://developers.openai.com/codex/noninteractive/),
and [app-server protocol](https://developers.openai.com/codex/app-server/).
Installed CLI/schema evidence establishes local interface availability; documentation
alone does not establish successful account authentication or semantic read-only access.

## Bounded probe and decision

A temporary, model-free client used the installed app-server protocol to send only
`initialize`, `initialized`, and `mcpServerStatus/list`. Discovery was limited to the
configured Atlassian server. No thread/turn, resource read, Jira lookup, Confluence
search, remote tool call, login, configuration edit or write operation was requested.
No managed repository was mounted or inspected.

The process reused the existing investigation Bubblewrap filesystem/PID boundary:
an ephemeral home, the existing Codex login file mounted read-only, no user config,
no unrelated home directories and no keyring bus. Only one native HTTP server was
injected, with a candidate read-tool allowlist, strict configuration parsing,
bounded startup and an overall deadline. Candidate tool names were not treated as
qualified just because configuration parsing accepted them. No MCP OAuth file was
available to mount; this does **not** establish that the account lacks valid
credentials in another supported store.

Observed result:

- Codex initialization succeeded.
- Atlassian catalog retrieval failed with a **network transport diagnostic**.
- Zero tools and no server capabilities were returned.
- Authentication remained `unknown`, not a verified authentication failure.
- No model turn or remote MCP tool call occurred.
- The controller terminated the temporary process after receiving the failed
  catalog result; its termination status is not an agent implementation failure.
- Only safe diagnostic status/counters were retained. Raw errors, headers,
  authorization URLs, credentials and private server responses were not exported.

This is enough to stop migration, not enough to conclude that MCP is fundamentally
unsupported or that the user's interactive connection is broken. The probe did not
establish Jira fields, Confluence retrieval, response completeness, OAuth refresh,
negative tool filtering or requirements provenance. No full investigation was run.

## Preconditions for replacing REST

1. Restore catalog connectivity on an authorized host while retaining the namespace
   boundary. Distinguish transport failure from authentication or approval failure.
2. Demonstrate reuse of the existing OAuth store without exposing unrelated secrets
   to model tools. Missing file-cache evidence is not a reason to export keyring
   credentials. A narrowly scoped credential broker may need separate review.
3. Inspect the actual catalog. Approve exact read-tool names and schemas; exclude
   generic dispatch/write tools and every unrelated server/plugin integration.
   Validate denied tools with a synthetic server, never a live Jira write attempt.
4. Prove unattended read behaviour under approval `never` and restricted shell
   networking. If native filtering cannot enforce this, assess the smallest read-only
   filtered MCP surface; do not implement a proxy before it is shown necessary.
5. Perform one bounded issue read, then a bounded explicitly linked page read.
   The live ticket read is permitted only after authentication and tool restrictions
   have been established. No unrestricted searches or full investigation are needed.
6. Capture structured MCP source responses with identifiers, complete original
   requirements, advertised revisions, retrieval timestamps where available and
   controller-computed hashes. Missing metadata must remain unknown; incomplete or
   truncated source evidence must block successful assessment acceptance.
7. Verify how installed `exec --json` exposes MCP result content and errors. Its
   documented MCP events are not proof of complete, untruncated provenance. Keep
   investigation-v1 validation and applicable instruction-chain validation.
8. Only then introduce a project provider selection, reject contradictory REST/MCP
   settings, adapt tests, and retire REST after feature/error-handling parity.

There is no `[projects.NAME.mcp]` option today. Unsupported settings are rejected,
not silently interpreted as read-only access. The synthetic configuration continues
to describe the existing REST route. No new Atlassian token is needed to perform
catalog qualification; the existing REST route still requires its configured
credentials. See [setup](setup.md#jira-investigation).

## Pilot status

An OAuth-only MCP investigation pilot is **blocked pending qualification**. Do not
remove sandbox restrictions, enable all user integrations, mount the whole home,
change global Codex settings or start the full investigation to work around this.
The smallest viable next step is a successful catalog-only qualification using the
existing connection in the intended isolation boundary. Keep the current REST
workflow available meanwhile; do not automatically fall back between providers or
claim a successful investigation based on process exit alone.

## Diagnostic follow-up: 2026-11-17

**Verdict:** the original zero-tool result was a transport failure under the
outer command sandbox: DNS failed before the remote service could be reached.
With narrowly authorized diagnostic network access, the same Bubblewrap boundary
reached Atlassian, but Codex reported **`notLoggedIn`** and authentication required.
These are two distinct blockers. There is no evidence that the host's existing
OAuth credentials are expired, invalid, or fundamentally incompatible with Codex.
Authenticated Atlassian discovery remains unqualified.

No production code, sandbox settings, managed repositories, global Codex
configuration or OAuth credentials were changed. No Jira/Confluence tool was
called, no model turn was started, and no new API credentials were requested.
This follow-up does not migrate providers or recommend a REST workaround.

### Evidence and bounds

The original local diagnostic export is retained at:

```text
results/MCP-QUALIFICATION/20261117T162142658203Z-codex-MCP-QUALIFICATION-c2a6eebb/probe.json
```

New safe observations and the exact diagnostic scripts are retained separately:

```text
results/MCP-QUALIFICATION/20261117T171636811568Z-codex-MCP-QUALIFICATION-01bbab05/
  diagnosis.json
  restricted-transport.json
  restricted-catalog_unfiltered.json
  authorized-transport.json
  authorized-catalog_unfiltered.json
  authorized-catalog_filtered.json
  restricted-fixture_allowlist.json
  restricted-fixture_prompt.json
  catalog_matrix_probe.py
  transport_matrix.py
  fixture_probe.py
  fixture_server.py
```

These are ignored, local qualification artifacts, not canonical task runs or
files distributed with a fresh clone. Existing canonical run storage and earlier
results are untouched. JSON contains status/counters and classified errors, not
raw MCP responses, tool descriptions, headers, authorization URLs or credentials.
The scripts capture raw process output only in memory and print selected safe
fields. Catalog probes have an 18-second deadline, 8-second server startup limit
and 2 MiB response limit; transport subprocesses have an 8-second outer timeout.
No managed repository is mounted. Controller termination (`-15`) after a reply
is intentional, not evidence of a failed model execution.

### Diagnostic matrix

| Check | Controlled comparison | Observed result | Conclusion and limit |
| --- | --- | --- | --- |
| A. Registration | Inspect only native Atlassian metadata; do not print whole configuration. | Enabled Streamable HTTP registration at the official `https://mcp.atlassian.com/v2/mcp` endpoint; no registration-level tool filters. Native CLI authentication metadata was `unknown`. | Registration exists. Metadata alone proves neither authentication nor discovery. |
| B. Model-free catalog | Installed `app-server --stdio --strict-config`; `initialize`, `initialized`, then `mcpServerStatus/list` with `serverName="atlassian"`, `detail="toolsAndAuthOnly"`, `limit=1`. | Initialization succeeds and the named server is returned; no model thread/turn or remote tool call is needed. | MCP discovery is available without a model. Atlassian initialization still fails as described below. |
| C. Explicit configuration | Ephemeral empty Codex home, the existing core login file read-only, explicit Atlassian-only server definition; plugins/apps disabled. | Strict configuration accepted; server appears with an error, not as an absent registration. | Skipping user configuration is not the zero-tool cause in this explicit probe. Production runners intentionally omit MCP and still skip user configuration. |
| D. Public transport | Credential-free DNS lookup and HTTPS HEAD, outside and inside unchanged Bubblewrap. No proxy, curl configuration, cookies or authentication; no redirects followed. | Restricted command sandbox: DNS lookup fails, curl exit 6. Narrow diagnostic network authorization: DNS succeeds and HTTPS returns 401 in both locations. | Original transport is blocked by the outer environment. HTTPS/TLS reaches the service when authorized; unauthenticated 401 is expected and says nothing about stored OAuth validity. |
| E. Authentication availability | Keep Bubblewrap filesystem mounts identical while permitting diagnostic transport. Check only host store existence and secret-service ownership. | Catalog changes from `unknown`/transport error to `notLoggedIn`/authentication required. Core `auth.json` exists; legacy `.credentials.json` and the newer `secrets` directory are absent. Host secret-service has an owner; its bus is not mounted into Bubblewrap. | The current namespace cannot resolve usable MCP OAuth. Host credential item existence, expiry, refresh and account permissions were not tested. Core Codex login does not establish MCP login. |
| F. Network isolation | Compare network-namespace identity and resolver-file hashes outside/inside Bubblewrap under each outer permission setting. | Both match; changing only diagnostic outer network authorization changes reachability. | This Bubblewrap setup does not unshare the network namespace. Codex shell-network policy and remote MCP transport authorization are separate controls. No production network policy was relaxed. |
| G. Catalog filtering | Repeat authorized Atlassian catalog request with and without candidate read-tool names. Separately use a synthetic local stdio MCP server. | Atlassian remains `notLoggedIn`, zero tools in both cases. Fixture allowlisting returns only the read tool and rejects an excluded tool call before it reaches the fixture. | Filtering is not the observed Atlassian failure. Native allowlisting works in the tested client path, but actual Atlassian read tools and semantic behavior remain unknown. |

### Authentication and transport interpretation

Installed help states that `--ignore-user-config` skips `$CODEX_HOME/config.toml`
while authentication still uses `CODEX_HOME`. `--ignore-rules` excludes execpolicy
files; it does not enable MCP or supply credentials. The actual application runners
retain both flags. The model-free app-server probe instead has an ephemeral empty
home and explicit overrides; it never reads the user's complete configuration.
Disabling plugins does not disable an explicitly configured native MCP server.

Codex supports native stdio and remote Streamable HTTP MCP registrations and
separate MCP OAuth storage. The version-matched
[OAuth implementation](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/rmcp-client/src/oauth.rs)
and [configuration reference](https://developers.openai.com/codex/config-reference/)
describe OS-keyring and file-backed storage. We checked store *availability*, not
credential contents. The host D-Bus check queried only the bus daemon's
`NameHasOwner("org.freedesktop.secrets")`; it did not query/unlock the secret
service or activate a login/refresh flow. A running secret service is consistent
with keyring-backed login but does not prove an Atlassian credential is stored.

The investigation namespace exposes only core Codex `auth.json`, not the MCP
OAuth cache, user config, host home or secret-service socket. It cannot reuse
OAuth merely because an interactive session can. Copying tokens or mounting the
entire credential store/session bus would cross the current trust boundary and
was not attempted. No claim about host OAuth validity or refresh is warranted.

The public endpoint check establishes DNS/TLS/HTTP reachability only. MCP
initialization, authenticated tool enumeration, Jira permissions and Confluence
permissions are stronger checks and did not pass. The authentication gate
stopped qualification before any ticket retrieval. Neither a Codex runtime
incompatibility nor an Atlassian outage has been established.

### Tool filtering and approval findings

The synthetic server advertises two no-op fixture tools, one annotated read-only
and one annotated destructive. Neither tool can change any file or external
system. With only `enabled_tools=["read_fixture"]` and no denylist, Codex's
catalog exposes only the read tool. After an ephemeral `thread/start` (no turn),
an explicit `mcpServer/tool/call` for the excluded tool returns an RPC error; the
fixture is not invoked. A direct call for the included tool succeeds. This tests
rejection, not merely the absence of a model-issued write call.

A second fixture probe, using `default_tools_approval_mode="prompt"` and
`approval_policy="never"`, still permits that explicit client read call and
rejects the excluded tool. These are **trusted app-server client calls**, not
model-generated calls. They do not qualify unattended model approval behavior;
neither approval settings nor read-only filesystem permissions replace a strict
remote-tool allowlist. The native `enabled_tools`/`disabled_tools` fields are
documented in [Codex MCP configuration](https://developers.openai.com/codex/mcp/).

An exact native allowlist can exclude unrelated/write tool names in the tested
path. It does not prove that an included tool has read-only semantics, safe
arguments, bounded discovery, or no generic dispatch capability. Atlassian's
actual catalog, tool schemas and these properties must still be reviewed. No
live write tool was called, even as a negative test. A tool-filtering proxy is
not justified yet: native rejection already works in the synthetic test.

### Remediation decision and next qualification

There is no small production fix supported by this evidence. Adding shell
network access would not make the hidden OAuth store available. Removing
`--ignore-user-config`, mounting the host home/session bus, or copying tokens
would weaken isolation without qualifying a safe read surface.

The smallest proposed architectural change is to separate the **trusted MCP
transport/authentication process** from sandboxed repository inspection. A
reviewed host-side component could use Codex's existing OAuth backend and expose
only approved MCP read capabilities through a narrow IPC interface. Repository
tools would retain the current Bubblewrap/Codex restrictions and receive no
credentials. Such a component must constrain authentication access, transport
destinations, tool identities/arguments, response sizes and provenance; it is a
design for review, **not an implemented broker or approved workaround**. No proxy
or additional credential store has been added.

To repeat the safe catalog-only check from this checkout on a host with authorized
MCP transport, adapt the old module imports in a temporary copy of the retained
script. The recorded diagnostic artifacts retain the imports used at the time.

```bash
evidence_path=results/MCP-QUALIFICATION/20261117T171636811568Z-codex-MCP-QUALIFICATION-01bbab05
probe_dir=$(mktemp -d)
sed -e 's/^from dev_agent\.agent import /from dev_agent.providers.codex import /' \
  -e 's/^from dev_agent\.investigation_agent import /from dev_agent.providers.codex_readonly import /' \
  "$evidence_path/catalog_matrix_probe.py" > "$probe_dir/catalog_matrix_probe.py"
env -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/dev-agent-uv-cache uv run python \
  "$probe_dir/catalog_matrix_probe.py" --unfiltered
```

This command preserves the existing namespace and does not grant network or
OAuth access. Under unchanged isolation, expect `notLoggedIn` when transport is
available, or a transport error when it is blocked. `--unfiltered` here means
inventory only: no model or tool call can use that catalog. Do not use an
unfiltered configuration for an actual investigation. Repeating this command
alone cannot resolve the OAuth boundary; the next development stage is review of
the scoped transport/authentication separation, then authenticated catalog and
tool qualification before any bounded ticket read.

All existing application/security tests remain required. The full suite and
lint/format results for this follow-up are recorded in `diagnosis.json`. The
fixture probes use no network, real credentials or managed repository. No
application changes were made, so no new application tests were needed.
