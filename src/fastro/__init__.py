"""fastro: the command-line tool that starts and maintains Fastro projects.

The CLI is a Typer application with two extension points:

- `bp.commands` entry-point group: third-party packages can register
  top-level Typer sub-apps that mount under `fastro <name>`.
- `bp.features` entry-point group: third-party packages can register
  ``Feature`` instances that ``bp feature`` can list, install, and remove.

In-tree commands and features live alongside this package and follow
the same contracts as plugins.
"""

from importlib.metadata import version

__version__ = version("fastro")
