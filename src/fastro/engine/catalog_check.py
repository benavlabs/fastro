"""``python -m fastro.engine.blueprint PATH``: track B's early check of its catalog and the ``fastro.toml`` next to it."""

from __future__ import annotations

import codecs
import sys
from importlib import metadata
from pathlib import Path

from packaging.version import Version

from fastro.engine import project
from fastro.engine.blueprint import Blueprint, CrossReferenceError, ExtendingCatalogError, IncompatibleCatalog, load
from fastro.engine.problems import CatalogError, Problem

COMMAND = "python -m fastro.engine.blueprint"
NOT_YET: tuple[str, ...] = ()
CROSS_REFERENCES_LEFT: tuple[str, ...] = ()


def main(argv: list[str]) -> int:
    """Check the ``blueprint.toml`` at ``argv[0]``, a repo or the file itself, and any ``fastro.toml`` next to it."""
    if len(argv) != 1:
        print(f"usage: {COMMAND} PATH", file=sys.stderr)
        return 2
    path = Path(argv[0])
    file = path / "blueprint.toml" if path.is_dir() else path
    if not file.is_file():
        why = "has no blueprint.toml" if path.is_dir() else "doesn't exist"
        print(f"error: {path} {why}. Pass a blueprint repo or its blueprint.toml: {COMMAND} PATH", file=sys.stderr)
        return 2
    try:
        tool = Version(metadata.version("fastro"))
    except metadata.PackageNotFoundError:
        print(
            "error: fastro isn't installed, so this can't tell which catalogs it reads. "
            "Install it (uv pip install -e PATH_TO_FASTRO), then run this again.",
            file=sys.stderr,
        )
        return 3
    beside = file.parent / "fastro.toml"
    try:
        loaded = load(str(file), _text(file), tool)
    except OSError as error:
        return _unreadable(file, error, path)
    except CatalogError as error:
        notes = []
        if not isinstance(error, IncompatibleCatalog | CrossReferenceError | ExtendingCatalogError):
            notes.append("Names that refer to another section aren't checked until these are fixed.")
        if beside.is_file():
            notes.append(f"{beside} is checked once {file} has no problems.")
        _report(error, file, path, notes)
        return 3 if isinstance(error, IncompatibleCatalog) else 1
    info = loaded.info
    features, points, presets = (
        _counted(len(loaded.features), "feature"),
        _counted(len(loaded.points), "point"),
        _counted(len(loaded.presets), "preset"),
    )
    print(f"ok: {file} is {info.name} {info.version}, with {features}, {points} and {presets}.")
    if beside.is_file() and (code := _project(beside, loaded, path)):
        return code
    print(not_checked(info.extends.name if info.extends is not None else None))
    return 0


def _project(file: Path, catalog: Blueprint, path: Path) -> int:
    """Check the ``fastro.toml`` next to ``catalog``, against it when it sets ``source = true`` (spec 3)."""
    try:
        text = _text(file)
        alone = project.load(str(file), text)
        if not alone.source:
            print(
                f"{file} doesn't set source = true, so it pins .fastro/blueprint.toml, not {catalog.document.file}, "
                "and this command doesn't check it there. A blueprint repo's fastro.toml sets source = true."
            )
            return 0
        loaded = project.load(str(file), text, catalog)
    except OSError as error:
        return _unreadable(file, error, path)
    except CatalogError as error:
        waiting = not isinstance(error, CrossReferenceError) and catalog.info.extends is None
        _report(error, file, path, ["Names that refer to the catalog aren't checked until these are fixed."] if waiting else [])
        return 1
    pin = loaded.blueprint
    print(f"ok: {file} is {loaded.name}, with {_counted(len(loaded.features), 'feature')}, for {pin.name} {pin.version}.")
    return 0


def _report(error: CatalogError, file: Path, path: Path, notes: list[str]) -> None:
    for line in [*map(str, error.problems), *notes]:
        print(line)
    count = len(error.problems)
    noun, them = ("problem", "it") if count == 1 else ("problems", "them")
    print(f"{count} {noun} in {file}. Fix {them}, then run {COMMAND} {path} again.")


def _unreadable(file: Path, error: OSError, path: Path) -> int:
    print(
        f"error: can't read {file}: {error.strerror}. Check its permissions, then run {COMMAND} {path} again.", file=sys.stderr
    )
    return 3


def not_checked(extends: str | None = None) -> str:
    """The line saying what ``main`` can't vouch for: :data:`NOT_YET`, cross-references and built-in shadowing.

    A catalog that ``extends`` another has its cross-references checked only once it's merged with its base.
    """
    sections = [f"[[{name}]]" if name == "wiring" else f"[{name}]" for name in NOT_YET]
    parts = [f"what's inside {_listed(sections)}"] if sections else []
    if extends is None:
        parts.extend(CROSS_REFERENCES_LEFT)
    else:
        parts.append(
            f"names that refer to another section, since this catalog extends {extends}, "
            "and those are checked on the merged catalog (extends, not built yet)"
        )
    parts.append("whether a declared command shadows a built-in fastro command")
    return f"Not checked yet: {_listed(parts)}."


def _text(file: Path) -> str:
    data = file.read_bytes().removeprefix(codecs.BOM_UTF8)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        line = data[: error.start].count(b"\n") + 1
        raise CatalogError(
            [Problem(str(file), line, "", "isn't UTF-8 text.", "Save it as UTF-8, then run this again.")]
        ) from None


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
