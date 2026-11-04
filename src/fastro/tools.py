"""The programs fastro runs (``git``, ``uv``) and its own installed version, and the errors when either is missing
(spec 8: exit 3)."""

from __future__ import annotations

import shutil
from importlib import metadata

from packaging.version import Version

from fastro.engine.catalog_check import NOT_INSTALLED


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


class NotInstalled(Exception):
    """fastro's own distribution isn't installed, so it can't tell which catalogs it reads (spec 8: exit 3)."""


def installed_version() -> Version:
    """The version of fastro that's running.

    Raises:
        NotInstalled: fastro isn't installed as a distribution.
    """
    try:
        return Version(metadata.version("fastro"))
    except metadata.PackageNotFoundError:
        raise NotInstalled(NOT_INSTALLED) from None
