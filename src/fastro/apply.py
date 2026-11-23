"""A repo's tracked files, and a plan's changes made in a directory: copies, writes and deletes, and nothing else."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from fastro.engine.files import file_kind, present
from fastro.engine.plan import Plan
from fastro.engine.spec import RepoPath
from fastro.tools import require

EXPORT_IGNORE = "export-ignore"
EXPORT_SUBST = "export-subst"


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


def tracked_files(repo: Path, untracked: bool = False) -> tuple[RepoPath, ...]:
    """The files git tracks in ``repo``, sorted by path in code point order, leaving out one deleted from the working
    tree and one ``export-ignore`` marks, as ``git archive`` does (spec 1.7, 2.13). With ``untracked``, the files git
    doesn't track or ignore are listed too, as ``--include-untracked`` asks (1.7). ``repo`` may be a directory inside a
    work tree: the paths are relative to it, and the work tree's own ``.gitattributes`` decide.

    One fastro can't check, in a directory it isn't allowed to search, is kept, so whoever reads it says why it can't.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        RepositoryError: ``repo`` isn't a git repository, or isn't in its work tree.
    """
    git = require("git")
    others = ["--others", "--exclude-standard"] if untracked else []
    listed = subprocess.run([git, "-C", str(repo), "ls-files", "-z", "--cached", *others], capture_output=True, text=True)
    if listed.returncode != 0:
        raise RepositoryError(f"{repo} isn't a git repository: {listed.stderr.strip()}")
    top, prefix = _place(repo)
    listed_names = {name.rstrip("/") for name in listed.stdout.split("\0") if name}
    names = sorted(name for name in listed_names if _in_working_tree(repo / name))
    directories = sorted({directory for name in names for directory in _above(prefix + name)})
    ignored = _set(Path(top), [*(prefix + name for name in names), *directories], EXPORT_IGNORE)
    return tuple(RepoPath(name) for name in names if ignored.isdisjoint([prefix + name, *_above(prefix + name)]))


def _above(name: str) -> list[str]:
    """Each directory ``name`` sits in, written as ``dir/``, which is how an ``export-ignore`` directory pattern
    matches it."""
    parts = name.split("/")[:-1]
    return [f"{'/'.join(parts[: count + 1])}/" for count in range(len(parts))]


def _place(repo: Path) -> tuple[Path, str]:
    """The top of the work tree ``repo`` sits in, and ``repo``'s path inside it as git writes a prefix (``a/b/``).

    Raises:
        RepositoryError: git says ``repo`` isn't in a work tree, quoting it.
    """
    git = require("git")
    where = subprocess.run(
        [git, "-C", str(repo), "rev-parse", "--show-toplevel", "--show-prefix"], capture_output=True, text=True
    )
    if where.returncode != 0:
        raise RepositoryError(f"{repo} isn't in a git work tree: {where.stderr.strip()}")
    top, prefix = where.stdout.split("\n")[:2]
    return Path(top), prefix


def head_commit(repo: Path) -> str | None:
    """The full commit ``HEAD`` names in the work tree ``repo`` sits in, or ``None`` before its first commit.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        RepositoryError: git says ``repo`` isn't in a work tree.
    """
    _place(repo)
    git = require("git")
    head = subprocess.run([git, "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True, text=True)
    return head.stdout.strip() if head.returncode == 0 else None


def export_subst(repo: Path, files: Iterable[RepoPath]) -> tuple[RepoPath, ...]:
    """Each of ``files`` that git's ``export-subst`` attribute is set on itself, by the ``.gitattributes`` files of
    the work tree ``repo`` sits in (spec 1.7), in the order given.

    Raises:
        MissingToolError: ``git`` isn't on ``PATH``.
        RepositoryError: git says ``repo`` isn't in a work tree.
    """
    given = [path.text for path in files]
    top, prefix = _place(repo)
    marked = _set(top, [prefix + name for name in given], EXPORT_SUBST)
    return tuple(RepoPath(name) for name in given if prefix + name in marked)


def _set(repo: Path, names: Sequence[str], attribute: str) -> set[str]:
    """Which of ``names`` ``attribute`` is set on, by the working tree's ``.gitattributes`` files alone, matched case
    by case as on Linux: never ``core.attributesFile``, the system's attributes, ``$GIT_DIR/info/attributes``, a git
    template's, or a repo the environment's git variables name (spec 1.7)."""
    if not names:
        return set()
    git = require("git")
    local = subprocess.run([git, "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True)
    unset = {*local.stdout.split(), "GIT_ATTR_SOURCE"}
    env = {name: value for name, value in os.environ.items() if name not in unset} | {"GIT_ATTR_NOSYSTEM": "1"}
    with tempfile.TemporaryDirectory(prefix="fastro-attributes-") as empty:
        subprocess.run([git, "init", "-q", "--bare", "--template=", empty], capture_output=True, check=True, env=env)
        checked = subprocess.run(
            [
                git,
                f"--git-dir={empty}",
                f"--work-tree={repo}",
                "-c",
                f"core.attributesFile={os.devnull}",
                "-c",
                "core.ignoreCase=false",
                "check-attr",
                "-z",
                "--stdin",
                attribute,
            ],
            input="\0".join(names) + "\0",
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
    fields = checked.stdout.split("\0")
    return {fields[index] for index in range(0, len(fields) - 2, 3) if fields[index + 2] == "set"}


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
