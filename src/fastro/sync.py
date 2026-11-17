"""``fastro sync`` in a blueprint repo: the wiring its own selection renders, written, deleted or diffed (spec 3)."""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from fastro.apply import Change, unreadable, unreadable_config
from fastro.engine import blueprint, catalog_check, project
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import plan
from fastro.engine.project import Project
from fastro.engine.render import render
from fastro.engine.resolve import select
from fastro.tools import installed_version

DEV_NULL = "/dev/null"


class UsageError(Exception):
    """``sync`` was run where it can't apply (spec 8: exit 2)."""


class Unreadable(Exception):
    """A file ``sync`` needs can't be read (spec 8: exit 3)."""

    def __init__(self, file: Path, error: OSError) -> None:
        super().__init__(f"can't read {file}: {error.strerror}. Check its permissions, then run fastro sync again.")


@dataclass(frozen=True)
class Source:
    """A blueprint repo's ``blueprint.toml`` and its own ``fastro.toml``, read from ``root``."""

    root: Path
    catalog: Blueprint
    project: Project


def load_source(root: Path) -> Source:
    """The blueprint repo at ``root``.

    Raises:
        UsageError: ``root`` has no ``fastro.toml``, it doesn't set ``source = true``, there's no
            ``blueprint.toml`` beside it, or either file is one fastro may not read (spec 1.10).
        Unreadable: either file can't be read.
        CatalogError: either file has a problem, or isn't UTF-8.
    """
    toml = root / "fastro.toml"
    if not os.path.lexists(toml):
        raise UsageError(f"{root} has no fastro.toml. Run fastro sync in a blueprint repo or a project.")
    if (refused := unreadable_config(root, "fastro.toml")) is not None:
        raise UsageError(refused)
    text = _read(toml)
    if not project.load("fastro.toml", text).source:
        raise UsageError(
            "fastro.toml doesn't set source = true, so this is a project, and fastro sync in a project isn't here yet."
        )
    file = root / "blueprint.toml"
    if not os.path.lexists(file):
        raise UsageError("fastro.toml sets source = true, but there's no blueprint.toml beside it.")
    if (refused := unreadable_config(root, "blueprint.toml")) is not None:
        raise UsageError(refused)
    catalog = blueprint.load("blueprint.toml", _read(file), installed_version())
    return Source(root, catalog, project.load("fastro.toml", text, catalog))


def wiring_changes(source: Source) -> list[Change]:
    """A write for each wiring file the repo's selection renders that differs from the one on disk, and a delete for
    each wiring file on disk that the selection doesn't render, by path."""
    loaded = source.project
    selection = select(source.catalog, loaded.features, loaded.app.requires, loaded.options)
    rendered = render(plan(source.catalog, selection, ()), loaded.app.contributes)
    found = []
    for file in sorted(source.catalog.wiring, key=lambda file: file.path.text):
        path = file.path.text
        if path in rendered:
            if _current(source.root, path) != rendered[path]:
                found.append(Change("write", path, rendered[path]))
        elif os.path.lexists(source.root / path):
            found.append(Change("delete", path))
    return found


def wiring_paths(source: Source) -> set[str]:
    """Every wiring path the catalog declares: the only files sync may change."""
    return {file.path.text for file in source.catalog.wiring}


def diff(root: Path, change: Change) -> str:
    """The unified diff ``change`` makes, from ``a/<path>`` to ``b/<path>``, with ``/dev/null`` for an absent side."""
    current = _current(root, change.path)
    before = f"a/{change.path}" if current is not None else DEV_NULL
    after = DEV_NULL if change.kind == "delete" else f"b/{change.path}"
    old = (current or "").splitlines(keepends=True)
    new = [] if change.kind == "delete" else change.text.splitlines(keepends=True)
    return "".join(difflib.unified_diff(old, new, before, after))


def _read(file: Path) -> str:
    try:
        return catalog_check.read_text(file)
    except OSError as error:
        raise Unreadable(file, error) from None


def _current(root: Path, path: str) -> str | None:
    """The text of ``path`` in ``root``, ``None`` when nothing is there or a symlink there leads to nothing, or a NUL,
    which no rendered file holds, when it isn't UTF-8 or isn't a file fastro may read (spec 1.10)."""
    if not (root / path).exists():
        return None
    if unreadable(root, path) is not None:
        return "\0"
    try:
        return (root / path).read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return "\0"
