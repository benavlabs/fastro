"""``fastro new``'s project writer (spec 3, 4): what a selection gives a project from an opened source, beside
``fastro.toml``, ``.fastro/blueprint.toml``, ``.fastro/lock.toml`` with each file's origin and sha256, and the base
snapshot, every refusal made before anything is written."""

from __future__ import annotations

import os
from collections.abc import Container, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from fastro import overlay, snapshot
from fastro.apply import ApplyError, Change, apply, check, same_file
from fastro.assembly import Marked, Unbuildable, assemble
from fastro.engine import lock as locking
from fastro.engine import project
from fastro.engine.blueprint import Blueprint
from fastro.engine.files import file_kind, shown, utf8
from fastro.engine.lock import LOCK_FILE, SCHEMA, Lock, Provenance
from fastro.engine.resolve import Selection
from fastro.engine.spec import RepoPath
from fastro.sources import CATALOG, Tree
from fastro.tools import installed_version

PROJECT_FILE = "fastro.toml"
KEPT_CATALOG = ".fastro/blueprint.toml"
NOTHING = "so nothing was written."


class Refused(Exception):
    """A project ``new`` won't write, refused before anything is (spec 8: exit 1)."""


@dataclass(frozen=True)
class Created:
    """Each path ``new`` wrote, and those among them where a file was already, both sorted by path."""

    written: tuple[str, ...]
    replaced: tuple[str, ...]


def _unmarked(files: Sequence[RepoPath]) -> Sequence[RepoPath]:
    return ()


def create(
    tree: Tree,
    catalog: Blueprint,
    selection: Selection,
    asked: Sequence[str],
    chosen: Mapping[tuple[str, str], str],
    name: str,
    target: Path,
    blueprint: Provenance,
    force: bool = False,
    marked: Marked = _unmarked,
    submodules: Container[str] = frozenset(),
) -> Created:
    """Write the project ``selection`` gives from ``tree`` into ``target``, named ``name``: what ``assemble`` gives,
    ``fastro.toml`` with the features in ``asked`` and the options in ``chosen`` (3), ``.fastro/blueprint.toml`` as
    the source holds it, ``.fastro/lock.toml`` recording ``blueprint``, the closed selection, every option and each
    file's origin and sha256, and the base snapshot with ``.fastro/.gitattributes`` (4). ``marked`` gives the files
    git's ``export-subst`` attribute is set on, and ``submodules`` the paths git records as a submodule.

    With ``force``, ``target`` may hold files already: each one ``new`` writes is replaced by a new file, and the rest
    stay (4).

    Raises:
        Refused: ``target`` is the source's own directory, is a file, or isn't empty without ``force``; the source
            contributes a ``.env`` the project would receive (2.1) or a file at a path that isn't UTF-8 (1.7);
            ``assemble`` refuses the build; two snapshot paths would be a file and a directory (4); or a file ``new``
            would write is one apply refuses. Nothing has been written.
        OSError: the system won't let fastro read the source or write the target (spec 8: exit 3).
    """
    if same_file(target, tree.root):
        raise Refused(
            f"{target} is the source's own directory, and fastro new won't write over the blueprint it reads, "
            f"{NOTHING} Name another directory."
        )
    kind = file_kind(target)
    if kind not in (None, "directory"):
        raise Refused(
            f"{target} is a file, and fastro new writes a project into a directory. Name one that's empty or doesn't exist yet."
        )
    if kind == "directory" and not force and any(target.iterdir()):
        raise Refused(f"{target} isn't empty. Name an empty directory, or pass --force to write into this one.")
    contributed = [path for path in tree.files if not any(only.covers(path.text) for only in catalog.info.all_blueprint_only)]
    if odd := next((path.text for path in contributed if not utf8(path.text)), None):
        raise Refused(
            f"{shown(odd)} is at a path that isn't UTF-8, which no source can carry, {NOTHING} Rename it to a path "
            "that's UTF-8."
        )
    if envs := overlay.env_files(catalog, tree.files, submodules, received=True):
        listed, them = overlay.listed(envs)
        raise Refused(
            f"{listed} would reach the project as a .env, which is almost always a leaked secret, {NOTHING} Remove "
            f"{them} from the blueprint: fastro new writes a project's .env itself."
        )
    try:
        built = assemble(tree.root, catalog, selection, tree.files, name, marked)
    except Unbuildable as error:
        raise Refused(str(error)) from None
    try:
        check(built.changes, tree.root, target)
    except ApplyError as error:
        raise Refused(str(error)) from None
    recorded = snapshot.recorded(built.changes, built.origins, tree.root)
    snapshots = snapshot.snapshot(built.changes, built.origins)
    _refuse_snapshot_clash(path.text for path in recorded if recorded[path].origin != snapshot.WIRING)
    locked = Lock(SCHEMA, installed_version(), blueprint, tuple(selection.features), dict(selection.options), files=recorded)
    found = [
        *built.changes,
        *snapshots,
        Change("write", PROJECT_FILE, project.written(catalog, asked, name, chosen)),
        Change("copy", KEPT_CATALOG, source=CATALOG),
        Change("write", LOCK_FILE, locking.dump(locked)),
    ]
    _refuse_own_files(tree, target, (change.path for change in found))
    try:
        checked = check(found, tree.root, target)
        replaced = sorted(change.path for change, destination, _ in checked if file_kind(destination) is not None)
        apply(found, tree.root, target)
    except ApplyError as error:
        raise Refused(str(error)) from None
    return Created(tuple(sorted(change.path for change in found)), tuple(replaced))


def _refuse_own_files(tree: Tree, target: Path, paths: Iterable[str]) -> None:
    """Refuse a path ``new`` would write in ``target`` that's one of the source's own files, as the file system sees
    it: a target inside the source, holding it, or the source spelled another way. The source's files are looked at
    only when one of those paths already holds a file."""
    there: dict[tuple[int, int], str] = {}
    for path in paths:
        with suppress(OSError):
            found = os.lstat(target / path)
            there.setdefault((found.st_dev, found.st_ino), path)
    if not there:
        return
    for file in tree.files:
        with suppress(OSError):
            found = os.lstat(tree.root / file.text)
            if (hit := there.get((found.st_dev, found.st_ino))) is not None:
                raise Refused(
                    f"{target / hit} is one of the source's own files, and fastro new won't write over the blueprint "
                    f"it reads, {NOTHING} Name a directory that holds none of them."
                )


def _refuse_snapshot_clash(paths: Iterable[str]) -> None:
    """Refuse two paths with a snapshot where one's snapshot path would be a directory the other's sits under: a
    file ``a`` and a file ``a.base/b`` (4)."""
    given = sorted(paths)
    snapshotted = {snapshot.base(path): path for path in given}
    for path in given:
        for above in overlay.parents(snapshot.base(path)):
            if (other := snapshotted.get(above)) is not None:
                raise Refused(
                    f"{other} and {path} would both have a snapshot, and {other}'s, {above}, would be a directory the "
                    f"other's sits under, {NOTHING}"
                )
