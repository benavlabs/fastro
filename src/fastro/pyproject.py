"""``{python_root}/pyproject.toml`` rewritten for a selection: its runtime dependencies and dev tables (spec 6.3)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import tomlkit
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from tomlkit.items import Array

from fastro.engine.blueprint import Blueprint
from fastro.engine.positions import KeyPath
from fastro.engine.resolve import Selection

BOM = "\ufeff"
CORE: KeyPath = ("blueprint", "dependencies")
MERGE_FIX = (
    "Give every entry for {name} the same environment marker, and the same direct reference when one is a direct "
    "reference, or drop all but one."
)

Entry = tuple[str, Requirement]


class PyprojectError(ValueError):
    """Runtime dependency entries for one package that spec 6.3 refuses to merge."""


def rewrite(text: str, catalog: Blueprint, selection: Selection) -> str:
    """``text``, a blueprint's ``pyproject.toml`` decoded as UTF-8 with any byte order mark it started with still at its
    start, with ``[project].dependencies`` the selection's runtime dependencies and each dev table pruned (6.3).

    Raises:
        PyprojectError: entries for one package have different environment markers, or one is a direct reference and
            they aren't all the same requirement.
    """
    bom = BOM if text.startswith(BOM) else ""
    document = tomlkit.parse(text.removeprefix(bom))
    merged = tomlkit.array()
    merged.extend(runtime(catalog, selection))
    merged.multiline(True)
    document["project"]["dependencies"] = merged
    declared, counted = _dev_names(catalog, selection)
    unwanted = declared - counted
    for table in _dev_tables(document):
        for index in reversed(range(len(table))):
            if (name := package(table[index])) is not None and name in unwanted:
                del table[index]
    return bom + tomlkit.dumps(document)


def runtime(catalog: Blueprint, selection: Selection) -> list[str]:
    """The selection's runtime dependencies as 6.3 writes them, one entry per package, sorted by name.

    Raises:
        PyprojectError: as :func:`rewrite` says.
    """
    groups: dict[str, list[Entry]] = {}
    for entry in _gathered(catalog, selection):
        groups.setdefault(canonicalize_name(entry[1].name), []).append(entry)
    return [_merged(groups[name]) for name in sorted(groups)]


def written(catalog: Blueprint, path: KeyPath, key: str | int) -> list[str]:
    """The strings of the list at ``path`` + ``key`` in ``blueprint.toml``, as written."""
    value: Any = catalog.document.data
    for part in (*path, key):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and isinstance(part, int):
            value = value[part]
        else:
            value = None
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _gathered(catalog: Blueprint, selection: Selection) -> Iterator[Entry]:
    yield from zip(written(catalog, CORE, "runtime"), catalog.info.dependencies, strict=True)
    for feature in catalog.features:
        if feature.name not in selection.features:
            continue
        yield from zip(written(catalog, feature.at, "dependencies"), feature.dependencies, strict=True)
        for when in feature.when:
            if selection.holds(when.condition):
                yield from zip(written(catalog, when.at, "dependencies"), when.dependencies, strict=True)
        for option in feature.options.values():
            value = selection.options[(feature.name, option.name)]
            chosen = option.dependencies.get(value, ())
            yield from zip(written(catalog, (*option.at, "dependencies"), value), chosen, strict=True)


def refusal(entries: Sequence[Entry]) -> str | None:
    """Why 6.3 refuses to merge ``entries``, all for one package, or ``None`` when it merges them."""
    requirement = entries[0][1]
    if any(other.url is not None for _, other in entries):
        if any(_same(other) != _same(requirement) for _, other in entries):
            return "has a direct reference, and its entries aren't the same requirement"
        return None
    if len({other.marker for _, other in entries}) > 1:
        return "has entries with different environment markers"
    return None


def _merged(entries: list[Entry]) -> str:
    first, requirement = entries[0]
    name = requirement.name
    if (refused := refusal(entries)) is not None:
        listed = ", ".join(repr(text) for text, _ in entries)
        normalized = canonicalize_name(name)
        raise PyprojectError(f"{normalized} {refused}: {listed}. {MERGE_FIX.format(name=normalized)}")
    if len(entries) == 1 or requirement.url is not None:
        return first
    extras = sorted(set().union(*(other.extras for _, other in entries)))
    specifier = SpecifierSet()
    for _, other in entries:
        specifier &= other.specifier
    joined = "[" + ",".join(extras) + "]" if extras else ""
    marker = f"; {requirement.marker}" if requirement.marker is not None else ""
    return f"{name}{joined}{specifier}{marker}"


def _same(requirement: Requirement) -> tuple[object, ...]:
    return canonicalize_name(requirement.name), frozenset(requirement.extras), requirement.url, requirement.marker


def _dev_names(catalog: Blueprint, selection: Selection) -> tuple[set[str], set[str]]:
    """The packages any feature or ``when`` declares as dev dependencies, and those that count for ``selection``."""
    declared: set[str] = set()
    counted: set[str] = set()
    for feature in catalog.features:
        selected = feature.name in selection.features
        for when in (None, *feature.when):
            names = {canonicalize_name(item.name) for item in (feature if when is None else when).dev_dependencies}
            declared |= names
            if selected and (when is None or selection.holds(when.condition)):
                counted |= names
    return declared, counted


def _dev_tables(document: tomlkit.TOMLDocument) -> list[Array]:
    project = document.get("project")
    extras = project.get("optional-dependencies") if isinstance(project, dict) else None
    groups = document.get("dependency-groups")
    tables = [table.get("dev") for table in (extras, groups) if isinstance(table, dict)]
    return [table for table in tables if isinstance(table, Array)]


def package(item: object) -> str | None:
    """The normalized package name of a dev table's ``item``, or ``None`` when it isn't a PEP 508 requirement string."""
    if not isinstance(item, str):
        return None
    try:
        return canonicalize_name(Requirement(item).name)
    except InvalidRequirement:
        return None
