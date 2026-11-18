"""The first step of the ``fastro`` and ``bp`` commands: refuse a current directory they can't use. ``fastro`` checks
before its CLI loads."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def check_cwd() -> None:
    """Exit 3 with fastro's own message when the system won't let it use the current directory (spec 1.37)."""
    try:
        os.getcwd()
    except OSError as error:
        reason = error.strerror or error
        sys.stderr.write(
            f"error: fastro can't use the current directory: {reason}. Run it from a directory that exists and you can open.\n"
        )
        sys.exit(3)


def main() -> None:
    """Check the current directory, then run the CLI."""
    check_cwd()
    from .cli import for_directory  # noqa: PLC0415

    for_directory(Path.cwd(), sys.argv[1:])()
