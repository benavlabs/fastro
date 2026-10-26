"""Run a module in a project's own interpreter, as a subprocess."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path


class MissingToolError(Exception):
    """A program a step needs isn't on ``PATH``."""

    def __init__(self, tool: str) -> None:
        super().__init__(tool)
        self.tool = tool


def module_file(python_root: Path, module: str) -> Path:
    """The file ``python -m module`` runs from ``python_root``."""
    return python_root.joinpath(*module.split(".")).with_suffix(".py")


def run_module(python_root: Path, module: str, args: Sequence[str] = ()) -> int:
    """Run ``python -m module args`` with ``uv run --project python_root``, from ``python_root``.

    Raises:
        MissingToolError: ``uv`` isn't on ``PATH``.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise MissingToolError("uv")

    command = [uv, "run", "--project", str(python_root), "python", "-m", module, *args]
    return subprocess.run(command, cwd=python_root, check=False).returncode
