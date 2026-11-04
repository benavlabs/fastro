"""The programs fastro runs (``git``, ``uv``), and the error when one isn't on ``PATH`` (spec 8: exit 3)."""

from __future__ import annotations

import shutil


class MissingToolError(Exception):
    """A program a step needs isn't on ``PATH``."""

    def __init__(self, tool: str) -> None:
        super().__init__(tool)
        self.tool = tool


def require(tool: str) -> str:
    """The path of ``tool`` on ``PATH``.

    Raises:
        MissingToolError: it isn't there.
    """
    found = shutil.which(tool)
    if found is None:
        raise MissingToolError(tool)
    return found
