"""Run the same CLI through python -m dev_agent and the console script.

The entry point forwards the controller's exit code to the calling process.
"""

from dev_agent.cli import main

raise SystemExit(main())
