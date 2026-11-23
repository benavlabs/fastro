"""A repo's tracked files, and a plan's changes made in a directory: copies, writes and deletes, and nothing else."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fastro.engine.files import file_kind, present
from fastro.engine.plan import Plan
from fastro.engine.spec import RepoPath
from fastro.tools import require


class RepositoryError(Exception):
    """A directory whose tracked files were asked for isn't a git repository."""


class ApplyError(Exception):
    """A change apply refuses to make: outside the target, through a symlink, not one this step may change, at a
    directory or anything else that isn't a regular file, or under a file where a directory should be."""


@dataclass(frozen=True)
class Change:
    """One change to a directory: ``copy`` a source file (the one at ``source``, or at ``path`` when ``source`` is
    empty), ``write`` ``text`` as UTF-8 with its newlines as given, or ``delete`` a file."""

    kind: str
    path: str
    text: str = ""
    source: str = ""


def tracked_files(repo: Path) -> tuple[RepoPath, ...]:
    """The files git tracks in ``repo``, sorted by path as git lists them, leaving out one deleted from the working tree.

    One fastro can't check, in a directory it isn't allowed to search, is kept, so whoever reads it says why it can't.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        RepositoryError: ``repo`` isn't a git repository.
    """
    git = require("git")
    listed = subprocess.run([git, "-C", str(repo), "ls-files", "-z"], capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        raise RepositoryError(f"{repo} isn't a git repository: {listed.stderr.strip()}")
    names = [name for name in listed.stdout.split("\0") if name and _in_working_tree(repo / name)]
    return tuple(RepoPath(name) for name in names)


def _in_working_tree(path: Path) -> bool:
    try:
        return present(path)
    except OSError:
        return True


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


def check(
    found: Iterable[Change], source: Path, target: Path, only: Collection[str] | None = None
) -> list[tuple[Change, Path, Path | None]]:
    """Each change with where it lands in ``target`` and, for a copy, the file it copies from ``source``, once every
    one is known to be allowed. Nothing is changed.

    A path must stay inside ``target`` once resolved, and no part of it there, nor of a copied file in ``source``,
    may be a symlink, and a copied file must be a regular file there. In ``target``, no part of the way to it may be a
    file, and what's already at it must be a regular file. With ``only``, each change's path must be one of them.

    Raises:
        ApplyError: a change breaks one of those rules.
    """
    checked = []
    for change in found:
        if only is not None and change.path not in only:
            raise ApplyError(f"{change.path!r} isn't a file this step may change, so nothing was changed.")
        destination = _inside(target, change.path, "the target")
        _regular_or_absent(target, destination, change.path)
        origin = None
        if change.kind == "copy":
            origin = _inside(source, change.source or change.path, "the source")
            _regular_source(origin, change.source or change.path)
        checked.append((change, destination, origin))
    return checked


def apply(found: Iterable[Change], source: Path, target: Path, only: Collection[str] | None = None) -> None:
    """Make each change in ``target``, copying from ``source``, once :func:`check` has allowed every one.

    Raises:
        ApplyError: a change isn't allowed. Nothing has been changed.
    """
    for change, destination, origin in check(found, source, target, only):
        if change.kind == "delete":
            if file_kind(destination) is not None:
                destination.unlink()
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin is not None:
            shutil.copyfile(origin, destination)
            shutil.copymode(origin, destination)
        else:
            destination.write_bytes(change.text.encode("utf-8"))


def _regular_source(origin: Path, path: str) -> None:
    """Refuse a copy whose file in the source is a directory or anything else that isn't a regular file."""
    kind = file_kind(origin)
    if kind == "directory":
        raise ApplyError(f"{path!r} is a directory in the source, so nothing was changed.")
    if kind != "file":
        raise ApplyError(f"{path!r} isn't a regular file in the source, so nothing was changed.")


def _regular_or_absent(target: Path, destination: Path, path: str) -> None:
    """Refuse a change whose way in ``target`` passes through a file, or whose path holds anything but a regular
    file."""
    base = target.resolve()
    current = base
    for part in destination.relative_to(base).parts[:-1]:
        current = current / part
        if file_kind(current) not in (None, "directory"):
            shown = current.relative_to(base).as_posix()
            raise ApplyError(f"{shown!r} is a file in the target where a directory should be, so nothing was changed.")
    kind = file_kind(destination)
    if kind == "directory":
        raise ApplyError(f"{path!r} is a directory in the target, so nothing was changed.")
    if kind not in (None, "file"):
        raise ApplyError(f"{path!r} isn't a regular file in the target, so nothing was changed.")


def _inside(root: Path, path: str, where: str) -> Path:
    """``root / path``, once it's known to stay inside ``root`` with no symlink on the way."""
    base = root.resolve()
    joined = Path(os.path.normpath(base / path))
    if not joined.is_relative_to(base) or joined == base:
        raise ApplyError(f"{path!r} resolves outside {where}, so nothing was changed.")
    current = base
    for part in joined.relative_to(base).parts:
        current = current / part
        if file_kind(current, follow=False) == "symlink":
            shown = current.relative_to(base).as_posix()
            raise ApplyError(f"{shown!r} is a symlink in {where}, and apply doesn't follow one, so nothing was changed.")
    return joined
