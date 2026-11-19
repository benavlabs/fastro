"""``fastro sync`` in a blueprint repo: the wiring its own selection renders, written, deleted or diffed (spec 3)."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from fastro.apply import Change, file_kind, present, unreadable, unreadable_config
from fastro.engine import blueprint, catalog_check, project
from fastro.engine.blueprint import Blueprint, ExtendingCatalogError
from fastro.engine.plan import plan
from fastro.engine.project import Project
from fastro.engine.render import render
from fastro.engine.resolve import select
from fastro.tools import installed_version

DEV_NULL = "/dev/null"
NO_CATALOG = (
    "there's no blueprint.toml beside fastro.toml. fastro {command} runs in a blueprint repo, which has both; in a "
    "project it isn't here yet."
)
EXTENDS = "blueprint.toml extends {name}, and this fastro can't merge an extending catalog with its base yet."


class UsageError(Exception):
    """``sync`` was run where it can't apply (spec 8: exit 2)."""


class Extending(Exception):
    """The catalog extends another, and this fastro can't merge the two yet (spec 2.10, 8: exit 3)."""


class Unreadable(Exception):
    """A file ``sync`` needs can't be read (spec 8: exit 3)."""

    def __init__(self, file: Path, error: OSError, command: str = "sync") -> None:
        super().__init__(f"can't read {file}: {error.strerror}. Check its permissions, then run fastro {command} again.")


@dataclass(frozen=True)
class Source:
    """A blueprint repo's ``blueprint.toml`` and its own ``fastro.toml``, read from ``root``."""

    root: Path
    catalog: Blueprint
    project: Project


def load_source(root: Path, command: str = "sync") -> Source:
    """The blueprint repo at ``root``, for ``fastro <command>``, read in spec 3's order: both files must be there,
    then ``blueprint.toml`` is loaded, then ``fastro.toml``.

    Raises:
        UsageError: ``root`` has no ``fastro.toml``, there's no ``blueprint.toml`` beside it, either file is one
            fastro may not read (spec 1.10), or ``fastro.toml`` doesn't set ``source = true``.
        Unreadable: either file can't be read.
        Extending: the catalog extends another, which this fastro can't merge yet (2.10).
        IncompatibleCatalog: the catalog's ``schema`` or ``requires_fastro`` rules out this fastro.
        CatalogError: either file has a problem, or isn't UTF-8.
    """
    toml = root / "fastro.toml"
    if not _present(toml, command):
        where = "a blueprint repo or a project" if command == "sync" else "a blueprint repo"
        raise UsageError(f"{root} has no fastro.toml. Run fastro {command} in {where}.")
    if (refused := _refused(root, "fastro.toml", command)) is not None:
        raise UsageError(refused)
    file = root / "blueprint.toml"
    if not _present(file, command):
        raise UsageError(NO_CATALOG.format(command=command))
    if (refused := _refused(root, "blueprint.toml", command)) is not None:
        raise UsageError(refused)
    try:
        catalog = blueprint.load("blueprint.toml", _read(file, command), installed_version())
    except ExtendingCatalogError:
        raise Extending(EXTENDS.format(name="another catalog")) from None
    if catalog.info.extends is not None:
        raise Extending(EXTENDS.format(name=catalog.info.extends.name))
    text = _read(toml, command)
    if not project.load("fastro.toml", text).source:
        raise UsageError(
            f"fastro.toml doesn't set source = true, so this is a project, and fastro {command} in a project isn't here yet."
        )
    return Source(root, catalog, project.load("fastro.toml", text, catalog))


def wiring_changes(source: Source) -> list[Change]:
    """A write for each wiring file the repo's selection renders that differs from the one on disk, and a delete for
    each wiring file on disk that the selection doesn't render, by path.

    Raises:
        Unreadable: the system won't let fastro look at a wiring path or open the file there (spec 8).
    """
    loaded = source.project
    selection = select(source.catalog, loaded.features, loaded.app.requires, loaded.options)
    rendered = render(plan(source.catalog, selection, ()), loaded.app.contributes)
    found = []
    for file in sorted(source.catalog.wiring, key=lambda file: file.path.text):
        path = file.path.text
        if path in rendered:
            if _current(source.root, path) != rendered[path]:
                found.append(Change("write", path, rendered[path]))
        elif _present(source.root / path, "sync"):
            found.append(Change("delete", path))
    return found


def wiring_paths(source: Source) -> set[str]:
    """Every wiring path the catalog declares: the only files sync may change."""
    return {file.path.text for file in source.catalog.wiring}


def diff(root: Path, change: Change) -> str:
    """The unified diff ``change`` makes, from ``a/<path>`` to ``b/<path>``, with ``/dev/null`` for an absent side.

    Raises:
        Unreadable: the system won't let fastro look at the path or open the file there (spec 8).
    """
    current = _current(root, change.path)
    before = f"a/{change.path}" if current is not None else DEV_NULL
    after = DEV_NULL if change.kind == "delete" else f"b/{change.path}"
    old = (current or "").splitlines(keepends=True)
    new = [] if change.kind == "delete" else change.text.splitlines(keepends=True)
    return "".join(difflib.unified_diff(old, new, before, after))


def _present(file: Path, command: str) -> bool:
    try:
        return present(file)
    except OSError as error:
        raise Unreadable(file, error, command) from None


def _refused(root: Path, name: str, command: str) -> str | None:
    try:
        return unreadable_config(root, name)
    except OSError as error:
        raise Unreadable(root / name, error, command) from None


def _read(file: Path, command: str) -> str:
    try:
        return catalog_check.read_text(file)
    except OSError as error:
        raise Unreadable(file, error, command) from None


def _current(root: Path, path: str) -> str | None:
    """The text of ``path`` in ``root``, ``None`` when nothing is there or a symlink there can't be followed to
    anything, or a NUL, which no rendered file holds, when it isn't UTF-8 or isn't a file fastro may read (spec 1.10).

    Raises:
        Unreadable: the system won't let fastro look at the path or open the file there (spec 8).
    """
    file = root / path
    try:
        if file_kind(file) is None:
            return None
        if unreadable(root, path) is not None:
            return "\0"
        return file.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return "\0"
    except OSError as error:
        raise Unreadable(file, error) from None
