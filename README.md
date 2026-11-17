# dev-agent

**[Quick Start and CLI reference](docs/quickstart.md)**

Dev Agent is a local CLI for analysing Jira requirements, implementing reviewed
tasks in isolated Git worktrees, and collecting verification evidence. Confirmed
publication can create a draft GitHub PR and observe checks on its exact commit.
The operator reviews and merges the PR.

**Current status:** `implement`, `verify` and standalone REST `investigate` are
available with their documented prerequisites. Draft publication and CI monitoring
are covered by synthetic tests. The one-command `deliver` workflow stops at the
unqualified Atlassian MCP intake gate.

## Install

From an authorised checkout of this repository on Ubuntu/Linux:

```bash
uv python install 3.14
uv sync --locked
uv run dev-agent --help
```

The Quick Start covers external tooling, authentication, project configuration,
usage and recovery.

- [Quick Start](docs/quickstart.md) for first use and the complete CLI reference.
- [Setup and operations](docs/setup.md) for configuration and safety procedures.
- [Architecture and workflow diagrams](docs/architecture.md) for contributors.
- [MCP qualification](docs/mcp-qualification.md) for intake security evidence.

## Development

```bash
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests use temporary repositories and simulated external providers. See the
[test layout](docs/architecture.md#test-layout) for focused suites.

## Licence recommendation

Apache-2.0 is recommended for permissive reuse with an explicit contributor patent
grant. No licence file has been added; the maintainer must approve the choice and
add the standard text/copyright details before publication. This repository does
not yet grant an open-source licence.
