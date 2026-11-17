"""fastro: the command-line tool that starts and maintains FastAPI projects.

The CLI is a Typer application. Third-party packages can register
top-level Typer sub-apps that mount under `fastro <name>` through the
`fastro.commands` entry-point group, the same contract the in-tree commands
follow.
"""

from importlib.metadata import version

__version__ = version("fastro")
