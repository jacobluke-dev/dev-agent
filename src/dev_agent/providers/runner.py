"""Define the runner interface and choose the current default adapters.

Delivery and investigation depend on these small factories. Concrete Codex invocation and
isolation remain in the provider modules.
"""

from pathlib import Path
from typing import Protocol

from dev_agent.config import AgentConfig
from dev_agent.process import ProcessResult
from dev_agent.providers.codex import CodexRunner
from dev_agent.providers.codex_readonly import CodexInvestigationRunner


class AgentRunner(Protocol):
    """Describe the preflight and execution operations consumed by workflows."""

    def preflight(self) -> dict[str, object]:
        """Check required execution facilities and return runtime provenance."""
        ...

    def run(
        self, worktree: Path, prompt: str, directory: Path, config: AgentConfig
    ) -> ProcessResult:
        """Execute the supplied prompt and retain outputs in the run directory."""
        ...


def implementation_runner() -> AgentRunner:
    """Create the default workspace-write Codex adapter for implementation."""
    return CodexRunner()


def read_only_runner() -> AgentRunner:
    """Create the default namespace-isolated adapter for investigation or review."""
    return CodexInvestigationRunner()
