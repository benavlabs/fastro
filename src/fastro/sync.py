"""``fastro sync`` in a blueprint repo: the wiring its own selection renders, written, deleted or diffed (spec 3)."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from fastro.apply import Change
from fastro.engine import blueprint, catalog_check, envfile, project
from fastro.engine.blueprint import Blueprint, Contributions, ExtendingCatalogError
from fastro.engine.files import breaks_the_rule, file_kind, present, unreadable, unreadable_config
from fastro.engine.plan import plan
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError
from fastro.engine.project import Project
from fastro.engine.render import as_ref, render, shadows
from fastro.engine.resolve import select
from fastro.engine.spec import Ref
from fastro.tools import installed_version

DEV_NULL = "/dev/null"
NO_NEWLINE = "\\ No newline at end of file"
NO_CATALOG = (
    "there's no blueprint.toml beside fastro.toml. fastro {command} runs in a blueprint repo, which has both; in a "
    "project it isn't here yet."
)
EXTENDS = "blueprint.toml extends {name}, and this fastro can't merge an extending catalog with its base yet."
CHECKED_ROOTS = ("scripts", "tests")
MODULE_FIX = "Point the ref at a module the repo has, or add its file."
ALIAS_FIX = "Give it an alias nothing else in that file binds."
CATALOG_ALIAS_FIX = "Give it, or what it clashes with, an alias nothing else in that file binds."


class _NotText:
    """What a file holds when it isn't UTF-8 text: unequal to any text, so it always differs (spec 3)."""


NOT_TEXT = _NotText()


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
    catalog = load_catalog(_read(file, command))
    text = _read(toml, command)
    if not project.load("fastro.toml", text).source:
        raise UsageError(
            f"fastro.toml doesn't set source = true, so this is a project, and fastro {command} in a project isn't here yet."
        )
    return Source(root, catalog, project.load("fastro.toml", text, catalog))


def load_catalog(text: str) -> Blueprint:
    """``text`` as ``blueprint.toml``, refused when it extends another catalog, whatever else it holds (spec 2.10).

    Raises:
        Extending: the catalog has a ``[blueprint].extends`` key.
        IncompatibleCatalog: the catalog's ``schema`` or ``requires_fastro`` rules out this fastro.
        CatalogError: the catalog has a problem.
        NotInstalled: fastro isn't installed.
    """
    try:
        catalog = blueprint.load("blueprint.toml", text, installed_version())
    except ExtendingCatalogError:
        raise Extending(EXTENDS.format(name="another catalog")) from None
    if catalog.info.extends is not None:
        raise Extending(EXTENDS.format(name=catalog.info.extends.name))
    return catalog


def wiring_changes(source: Source) -> list[Change]:
    """A write for each wiring file the repo's selection renders that differs from the one on disk, and a delete for
    each wiring file on disk that the selection doesn't render, by path.

    Raises:
        CatalogError: an ``[app].contributes`` ref names a module the repo doesn't have, or it or a catalog
            contribution binds a name its wiring file binds otherwise (3).
        Unreadable: the system won't let fastro look at a wiring path or a module file, or open a wiring file (spec 8).
    """
    loaded = source.project
    selection = select(source.catalog, loaded.features, loaded.app.requires, loaded.options)
    made = plan(source.catalog, selection, ())
    rendered = render(made, loaded.app.contributes)
    problems = [
        loaded.document.problem(at, f"{str(ref)!r} {reason}", MODULE_FIX)
        for at, ref in app_refs(source.catalog, loaded.app.contributes)
        if (reason := _missing_module(source.root, source.catalog, ref)) is not None
    ]
    shadowed = shadows(made, loaded.app.contributes, loaded.document.line)
    problems += [loaded.document.problem(at, message, ALIAS_FIX) for at, message in shadowed.app]
    problems += [source.catalog.document.problem(at, message, CATALOG_ALIAS_FIX) for at, message in shadowed.catalog]
    if problems:
        raise CatalogError(problems)
    found = []
    for file in sorted(source.catalog.wiring, key=lambda file: file.path.text):
        path = file.path.text
        if path in rendered:
            if _current(source.root, path) != rendered[path]:
                found.append(Change("write", path, rendered[path]))
        elif _present(source.root / path, "sync"):
            found.append(Change("delete", path))
    return found


def app_refs(catalog: Blueprint, contributes: Contributions) -> list[tuple[KeyPath, Ref]]:
    """Each ``[app].contributes`` ref to a point no ``literal`` or ``data`` slot collects, with where it's written."""
    values = {slot.point for file in catalog.wiring for slot in file.slots if slot.literal or slot.kind == "data"}
    return [(item.at, as_ref(item.target)) for point, items in contributes.items() if point not in values for item in items]


def module_files(catalog: Blueprint, ref: Ref) -> tuple[str, str] | None:
    """The two files ``ref``'s module can be, ``<parts>.py`` and ``<parts>/__init__.py`` under ``python_root``, or
    ``None`` when its first part isn't ``import_root``, ``scripts`` or ``tests`` and it isn't checked (2.13)."""
    parts = ref.module.split(".")
    if parts[0] not in (catalog.info.import_root, *CHECKED_ROOTS):
        return None
    base = f"{catalog.info.python_root.text.rstrip('/')}/{'/'.join(parts)}"
    return f"{base}.py", f"{base}/__init__.py"


def _missing_module(root: Path, catalog: Blueprint, ref: Ref) -> str | None:
    """Why ``ref``'s module has no file in the repo that 1.10 doesn't rule out, or ``None`` when it has one or isn't
    checked (3).

    Raises:
        Unreadable: the system won't let fastro look at either file.
    """
    files = module_files(catalog, ref)
    if files is None:
        return None
    for file in files:
        try:
            if breaks_the_rule(root, file) is None:
                return None
        except OSError as error:
            raise Unreadable(root / file, error) from None
    return f"names a module the repo doesn't have: neither {files[0]} nor {files[1]} is there."


def wiring_paths(source: Source) -> set[str]:
    """Every wiring path the catalog declares: the only files sync may change."""
    return {file.path.text for file in source.catalog.wiring}


def diff(root: Path, change: Change) -> str:
    """The unified diff ``change`` makes, from ``a/<path>`` to ``b/<path>``, with ``/dev/null`` for an absent side, or
    git's one line for a binary file when the file there isn't UTF-8.

    Raises:
        Unreadable: the system won't let fastro look at the path or open the file there (spec 8).
    """
    current = _current(root, change.path)
    before = f"a/{change.path}" if current is not None else DEV_NULL
    after = DEV_NULL if change.kind == "delete" else f"b/{change.path}"
    if isinstance(current, _NotText):
        return f"Binary files {before} and {after} differ\n"
    old = _lines(current or "")
    new = [] if change.kind == "delete" else _lines(change.text)
    return "".join(
        line if line.endswith("\n") else f"{line}\n{NO_NEWLINE}\n" for line in difflib.unified_diff(old, new, before, after)
    )


def _lines(text: str) -> list[str]:
    """``text`` split after each ``\\n`` only, as ``patch`` reads it, each line keeping its ending."""
    lines = [f"{line}\n" for line in envfile.lines(text)]
    lines[-1] = lines[-1].removesuffix("\n")
    return lines if lines[-1] else lines[:-1]


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


def _current(root: Path, path: str) -> str | _NotText | None:
    """The text of ``path`` in ``root``, ``None`` when nothing is there or a symlink there can't be followed to
    anything, or :data:`NOT_TEXT` when it isn't UTF-8 or isn't a file fastro may read (spec 1.10).

    Raises:
        Unreadable: the system won't let fastro look at the path or open the file there (spec 8).
    """
    file = root / path
    try:
        if file_kind(file) is None:
            return None
        if unreadable(root, path) is not None:
            return NOT_TEXT
        return file.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return NOT_TEXT
    except OSError as error:
        raise Unreadable(file, error) from None
