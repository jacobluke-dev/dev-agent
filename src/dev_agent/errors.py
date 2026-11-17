"""Define errors that cross Dev Agent's execution boundaries.

Workflows and the CLI use these types to distinguish actionable refusals from failures to
establish the mandatory provider sandbox.
"""


class DevAgentError(Exception):
    """An actionable configuration, safety, or execution error."""


class IsolationError(DevAgentError):
    """Indicate that a runner could not establish the required execution isolation."""

    pass
