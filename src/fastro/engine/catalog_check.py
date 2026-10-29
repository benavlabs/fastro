"""``python -m fastro.engine.blueprint PATH``: track B's early check of its catalog, before the CLI exists."""

from __future__ import annotations

import codecs
import sys
from importlib import metadata
from pathlib import Path

from packaging.version import Version

from fastro.engine.blueprint import CrossReferenceError, ExtendingCatalogError, IncompatibleCatalog, load
from fastro.engine.problems import CatalogError, Problem

COMMAND = "python -m fastro.engine.blueprint"
NOT_YET: tuple[str, ...] = ()
CROSS_REFERENCES_LEFT: tuple[str, ...] = ()


def main(argv: list[str]) -> int:
    """Check the ``blueprint.toml`` at ``argv[0]``, a repo or the file itself, printing ``ok`` or its problems."""
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
    try:
        loaded = load(str(file), _text(file), tool)
    except OSError as error:
        print(
            f"error: can't read {file}: {error.strerror}. Check its permissions, then run {COMMAND} {path} again.",
            file=sys.stderr,
        )
        return 3
    except CatalogError as error:
        for problem in error.problems:
            print(problem)
        if not isinstance(error, IncompatibleCatalog | CrossReferenceError | ExtendingCatalogError):
            print("Names that refer to another section aren't checked until these are fixed.")
        count = len(error.problems)
        noun, them = ("problem", "it") if count == 1 else ("problems", "them")
        print(f"{count} {noun} in {file}. Fix {them}, then run {COMMAND} {path} again.")
        return 3 if isinstance(error, IncompatibleCatalog) else 1
    info = loaded.info
    features, points, presets = (
        _counted(len(loaded.features), "feature"),
        _counted(len(loaded.points), "point"),
        _counted(len(loaded.presets), "preset"),
    )
    print(f"ok: {file} is {info.name} {info.version}, with {features}, {points} and {presets}.")
    print(not_checked(info.extends.name if info.extends is not None else None))
    return 0


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
