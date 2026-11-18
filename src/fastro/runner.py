"""Run a module in a project's own interpreter, as a subprocess: the declared commands (spec 2.9) among them."""

from __future__ import annotations

import secrets
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from fastro.engine.blueprint import Blueprint, Command
from fastro.engine.resolve import Selection
from fastro.engine.spec import RepoPath
from fastro.tools import require

TOKEN_BYTES = range(16, 129)
TOKEN_DEFAULT = 32


class OutsideProject(Exception):
    """A declared command that runs only in a project was run outside one."""


class TokenUsageError(Exception):
    """A ``token_hex`` command run outside a project was given something other than ``--bytes N`` (spec 8: exit 2)."""


def available(catalog: Blueprint, selection: Selection) -> dict[tuple[str, ...], Command]:
    """The commands ``selection`` has, by their words: every top-level one, and each selected feature's (2.9)."""
    found = dict(catalog.commands)
    for feature in catalog.features:
        if feature.name in selection.features:
            found.update(feature.commands)
    return found


def run_declared(command: Command, project: Path | None, python_root: RepoPath, args: Sequence[str]) -> int:
    """Run ``command`` in ``project`` and return its exit code, or, outside a project, print its ``token_hex``.

    In a project it runs ``uv run --project {python_root} python -m <module> <args…>`` from the command's ``cwd``,
    which defaults to ``python_root``.

    Raises:
        OutsideProject: ``project`` is ``None`` and the command doesn't print a ``token_hex`` there.
        TokenUsageError: it does, and ``args`` isn't ``--bytes N`` with N from 16 to 128.
        MissingToolError: ``uv`` isn't on ``PATH``.
    """
    words = " ".join(command.words)
    if project is None:
        if command.outside_project != "token_hex":
            raise OutsideProject(f"fastro {words} runs in a project. Run it from inside one.")
        print(secrets.token_hex(_token_bytes(words, args)))
        return 0
    argv, cwd = declared_command(command, project, python_root, args)
    return subprocess.run(argv, cwd=cwd, check=False).returncode


def declared_command(command: Command, project: Path, python_root: RepoPath, args: Sequence[str]) -> tuple[list[str], Path]:
    """The argv and working directory ``command`` runs with in ``project``: ``uv run --project {python_root} python -m
    <module> <args…>``, from the command's ``cwd`` (default ``python_root``), with every path absolute.

    Raises:
        MissingToolError: ``uv`` isn't on ``PATH``.
    """
    root = project.resolve()
    backend = root / python_root.text.rstrip("/")
    cwd = root / command.cwd.text.rstrip("/") if command.cwd is not None else backend
    return [require("uv"), "run", "--project", str(backend), "python", "-m", command.module, *args], cwd


def run_captured(argv: Sequence[str], cwd: Path, environment: Mapping[str, str]) -> tuple[int, str]:
    """Run ``argv`` from ``cwd`` with only ``environment``, and return its exit code and what it printed, stdout then
    stderr, stripped.

    Raises:
        OSError: its program can't be started, such as ``FileNotFoundError`` when it isn't on ``PATH``.
    """
    completed = subprocess.run(list(argv), cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False)
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def _token_bytes(words: str, args: Sequence[str]) -> int:
    """N from ``--bytes N`` or ``--bytes=N``, or 32 with no arguments."""
    refused = TokenUsageError(f"fastro {words} outside a project takes only --bytes N, an integer from 16 to 128.")
    if not args:
        return TOKEN_DEFAULT
    if len(args) == 2 and args[0] == "--bytes":
        given = args[1]
    elif len(args) == 1 and args[0].startswith("--bytes="):
        given = args[0].removeprefix("--bytes=")
    else:
        raise refused
    if not (given.isascii() and given.isdigit()) or int(given) not in TOKEN_BYTES:
        raise refused
    return int(given)
