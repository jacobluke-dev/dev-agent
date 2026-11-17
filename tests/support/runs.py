"""Helpers for constructing test checks and substituting an inert implementation runner."""

import sys
from dataclasses import replace
from unittest.mock import Mock

from dev_agent.config import Check, Config
from dev_agent.process import ProcessResult


def configured(config: Config, **settings: object) -> Config:
    """Replace selected settings on the synthetic project without reloading TOML."""
    return replace(config, projects={"example": replace(config.project("example"), **settings)})


def runner() -> Mock:
    """Return an inert successful implementation runner that tests can override."""
    agent = Mock()
    agent.preflight.return_value = {"provider": "codex", "version": "fake"}
    agent.run.return_value = ProcessResult(0, 0.01)
    return agent


def python_check(name: str, code: str, **settings: object) -> Check:
    """Build a trusted fixture command invoking the current Python executable."""
    return Check(name, (sys.executable, "-c", code), **settings)
