"""The ``fastro`` command's first step: refuse a current directory it can't use, before the CLI loads."""

from __future__ import annotations

import importlib
import os
import sys


def main() -> None:
    """Exit 3 when the system won't let fastro use the current directory (spec 1.37), and run the CLI otherwise."""
    try:
        os.getcwd()
    except OSError as error:
        reason = error.strerror or error
        sys.stderr.write(
            f"error: fastro can't use the current directory: {reason}. Run it from a directory that exists and you can open.\n"
        )
        sys.exit(3)
    importlib.import_module("fastro.cli").app()
