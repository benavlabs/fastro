"""A repo's tracked files, and a plan's changes made in a directory: copies, writes and deletes, and nothing else."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fastro.engine.plan import Plan
from fastro.engine.spec import RepoPath
from fastro.tools import require


class RepositoryError(Exception):
    """A directory whose tracked files were asked for isn't a git repository."""


class ApplyError(Exception):
    """A change apply refuses to make: outside the target, through a symlink, or not one this step may change."""


@dataclass(frozen=True)
class Change:
    """One change to a directory: ``copy`` a source file, ``write`` ``text`` as UTF-8 with its newlines as given, or
    ``delete`` a file."""

    kind: str
    path: str
    text: str = ""


def tracked_files(repo: Path) -> tuple[RepoPath, ...]:
    """The files git tracks in ``repo``, sorted by path as git lists them, leaving out one deleted from the working tree.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        RepositoryError: ``repo`` isn't a git repository.
    """
    git = require("git")
    listed = subprocess.run([git, "-C", str(repo), "ls-files", "-z"], capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        raise RepositoryError(f"{repo} isn't a git repository: {listed.stderr.strip()}")
    names = [name for name in listed.stdout.split("\0") if name and os.path.lexists(repo / name)]
    return tuple(RepoPath(name) for name in names)


def changes(plan: Plan, rendered: Mapping[str, str]) -> list[Change]:
    """The changes ``plan`` makes: a copy for each ``copy`` op, a write of ``rendered`` text for each ``render`` op,
    and a delete for each ``delete`` op, in the plan's order. A ``skip`` changes nothing."""
    found = []
    for op in plan.ops:
        if op.op == "copy":
            found.append(Change("copy", op.path))
        elif op.op == "render":
            found.append(Change("write", op.path, rendered[op.path]))
        elif op.op == "delete":
            found.append(Change("delete", op.path))
    return found


def apply(found: Iterable[Change], source: Path, target: Path, only: Collection[str] | None = None) -> None:
    """Make each change in ``target``, copying from ``source``, once every one has been checked.

    A path must stay inside ``target`` once resolved, and no part of it there, nor of a copied file in ``source``,
    may be a symlink. With ``only``, each change's path must be one of them.

    Raises:
        ApplyError: a change breaks one of those rules. Nothing has been changed.
    """
    checked = []
    for change in found:
        if only is not None and change.path not in only:
            raise ApplyError(f"{change.path!r} isn't a file this step may change, so nothing was changed.")
        destination = _inside(target, change.path, "the target")
        origin = _inside(source, change.path, "the source") if change.kind == "copy" else None
        checked.append((change, destination, origin))
    for change, destination, origin in checked:
        if change.kind == "delete":
            if destination.exists():
                destination.unlink()
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin is not None:
            shutil.copyfile(origin, destination)
            shutil.copymode(origin, destination)
        else:
            destination.write_bytes(change.text.encode("utf-8"))


def _inside(root: Path, path: str, where: str) -> Path:
    """``root / path``, once it's known to stay inside ``root`` with no symlink on the way."""
    base = root.resolve()
    joined = Path(os.path.normpath(base / path))
    if not joined.is_relative_to(base) or joined == base:
        raise ApplyError(f"{path!r} resolves outside {where}, so nothing was changed.")
    current = base
    for part in joined.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            shown = current.relative_to(base).as_posix()
            raise ApplyError(f"{shown!r} is a symlink in {where}, and apply doesn't follow one, so nothing was changed.")
    return joined
