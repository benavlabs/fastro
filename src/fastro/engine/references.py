"""Names in a loaded ``blueprint.toml`` that refer to another section, checked once every key reads cleanly."""

from __future__ import annotations

import difflib
import string
from collections.abc import Collection, Iterator
from typing import TYPE_CHECKING

from fastro.engine import conditions
from fastro.engine.conditions import Condition
from fastro.engine.positions import KeyPath
from fastro.engine.problems import Problem, key_name
from fastro.engine.spec import Ref, SpecError

if TYPE_CHECKING:
    from fastro.engine.blueprint import Blueprint, Contribution, Contributions, Slot

BUILT_IN_FIELDS = frozenset({"name", "ref", "module", "attr", "value"})
ONE_SLOT_KINDS = ("value", "reexport")


def problems(catalog: Blueprint) -> list[Problem]:
    """Every name in ``catalog`` that refers to something it doesn't declare, a ``requires`` cycle, or a point,
    slot or contribution that doesn't fit the others (spec 2.3 and 2.6)."""
    return list(_Checker(catalog).run())


class _Checker:
    def __init__(self, catalog: Blueprint) -> None:
        self.catalog = catalog
        self.features = {feature.name: feature for feature in catalog.features}
        self.points = catalog.points
        self.slots: dict[str, Slot] = {}
        for file in catalog.wiring:
            for slot in file.slots:
                if slot.kind != "raw" and slot.point in self.points:
                    self.slots.setdefault(slot.point, slot)

    def problem(self, path: KeyPath, message: str, fix: str) -> Problem:
        return self.catalog.document.problem(path, message, fix)

    def run(self) -> Iterator[Problem]:
        catalog = self.catalog
        groups: list[tuple[KeyPath, str, Contributions]] = []
        for feature in catalog.features:
            for index, name in enumerate(feature.requires):
                yield from self.feature((*feature.at, "requires", index), name)
            groups.append(((*feature.at, "contributes"), feature.name, feature.contributes))
            for index, when in enumerate(feature.when):
                yield from self.condition((*feature.at, "when", index, "if"), when.condition)
                groups.append(((*feature.at, "when", index, "contributes"), feature.name, when.contributes))
        yield from self.cycles()
        groups.append((("core", "contributes"), "[core]", catalog.core))
        for name, generator in catalog.generators.items():
            if generator.register is not None:
                groups.append((("generators", name, "register", "contributes"), "", generator.register.parsed))
        for path, _, contributes in groups:
            yield from self.contributions(path, contributes)
        yield from self.owners(groups)
        yield from self.wiring()
        for name, members in catalog.presets.items():
            for index, member in enumerate(members):
                yield from self.feature(("presets", name, index), member)
        for name, members in catalog.verify.sets.items():
            for index, member in enumerate(members):
                yield from self.feature(("verify", "sets", name, index), member)
        for check in catalog.verify.checks:
            yield from self.condition((*check.at, "if"), check.condition)
        for file in catalog.wiring:
            yield from self.condition((*file.at, "if"), file.condition)
        if catalog.plugin_api is not None:
            for name in catalog.plugin_api.public_with:
                yield from self.feature((*catalog.plugin_api.at, "public_with", name), name)
            for index, point in enumerate(catalog.plugin_api.points):
                yield from self.plugin_point((*catalog.plugin_api.at, "points", index), point)

    def feature(self, path: KeyPath, name: str) -> Iterator[Problem]:
        if name not in self.features:
            listing = "The features are {}." if self.features else "This catalog declares no features yet."
            fix = _closest(name, self.features, listing)
            yield self.problem(path, f"{name!r} isn't a feature of this catalog.", fix)

    def point(self, path: KeyPath, name: str) -> Iterator[Problem]:
        if name not in self.points:
            listing = "The points are {}." if self.points else "This catalog declares no points yet."
            yield self.problem(path, f"{name!r} isn't a declared point.", _closest(name, self.points, listing))

    def contributions(self, path: KeyPath, contributes: Contributions) -> Iterator[Problem]:
        for point, items in contributes.items():
            yield from self.point((*path, point), point)
            slot = self.slots.get(point)
            for index, item in enumerate(items):
                if slot is not None:
                    yield from self.fits((*path, point, index), item, slot)
                yield from self.condition((*path, point, index, "if"), item.condition)

    def fits(self, path: KeyPath, item: Contribution, slot: Slot) -> Iterator[Problem]:
        forms: tuple[str, ...]
        if slot.literal:
            forms, wanted, kind = ("", "value"), "a string or a value table", "literal"
        elif slot.kind == "data":
            forms, wanted, kind = ("data",), "a data table", "data"
        else:
            forms, wanted, kind = ("", "ref"), "a ref", slot.kind
        if item.via not in forms:
            yield self.problem(path, f"should be {wanted}, since {slot.point} is collected by a {kind} slot.", "")
            return
        if item.via == "" and not slot.literal and isinstance(item.target, str):
            try:
                Ref.parse(item.target)
            except SpecError as error:
                yield self.problem(path, str(error), "")
                return
        given = BUILT_IN_FIELDS | set(slot.defaults) | set(item.fields)
        for name in _fields(slot.item):
            if name not in given:
                message = f"doesn't give {name}, which the item of {key_name(slot.at)} uses."
                yield self.problem(
                    path, message, f"Add {name} to this contribution, or a default for it in that slot's defaults."
                )

    def owners(self, groups: list[tuple[KeyPath, str, Contributions]]) -> Iterator[Problem]:
        line = self.catalog.document.line
        claims = sorted(
            (line((*path, point)), (*path, point), point, owner)
            for path, owner, contributes in groups
            for point in contributes
            if owner and point in self.points and self.points[point].cardinality == "one"
        )
        first: dict[str, str] = {}
        for _, path, point, owner in claims:
            if first.setdefault(point, owner) != owner:
                yield self.problem(path, f"{point!r} is a one point, and {first[point]} contributes to it already.", "")

    def wiring(self) -> Iterator[Problem]:
        seen: dict[str, KeyPath] = {}
        for file in self.catalog.wiring:
            for slot in file.slots:
                if slot.kind == "raw":
                    continue
                path = (*slot.at, "point")
                if slot.point not in self.points:
                    yield from self.point(path, slot.point)
                elif slot.point in seen:
                    yield self.problem(path, f"{slot.point!r} is collected by {key_name(seen[slot.point])} already.", "")
                else:
                    seen[slot.point] = slot.at
                    if slot.kind in ONE_SLOT_KINDS and self.points[slot.point].cardinality != "one":
                        message = f"{slot.point!r} is a many point, and a {slot.kind} slot collects a one point."
                        fix = f'Set cardinality = "one" in [points.{slot.point}], or collect it with a tuple or list slot.'
                        yield self.problem(path, message, fix)
        for name, point in self.points.items():
            if name not in seen:
                fix = f"Add a slot with point = {name!r} to a [[wiring]] file, or drop the point."
                yield self.problem(point.at, "isn't collected by any slot.", fix)

    def plugin_point(self, path: KeyPath, name: str) -> Iterator[Problem]:
        if name not in self.points:
            yield from self.point(path, name)
        elif self.points[name].cardinality != "many":
            message = f"{name!r} is a one point, and plugins contribute only to many points."
            fix = f'Set cardinality = "many" in [points.{name}], or leave it out of plugin_api.points.'
            yield self.problem(path, message, fix)

    def condition(self, path: KeyPath, condition: Condition | None) -> Iterator[Problem]:
        if condition is None:
            return
        unknown: set[str] = set()
        for atom in dict.fromkeys(conditions.atoms(condition)):
            name = atom.feature if isinstance(atom, conditions.Option) else atom.name
            if name not in self.features and name not in unknown:
                unknown.add(name)
                yield from self.feature(path, name)
            elif isinstance(atom, conditions.Option) and name in self.features:
                yield from self.option(path, atom)

    def option(self, path: KeyPath, atom: conditions.Option) -> Iterator[Problem]:
        options = self.features[atom.feature].options
        if atom.option not in options:
            listing = f"The options of {atom.feature} are {{}}." if options else f"{atom.feature} has no options."
            fix = _closest(atom.option, options, listing)
            yield self.problem(path, f"'{atom.feature}.{atom.option}' isn't an option of {atom.feature}.", fix)
        elif atom.value not in options[atom.option].choices:
            choices = ", ".join(options[atom.option].choices)
            message = f"{atom.value!r} isn't a choice of {atom.feature}.{atom.option}."
            yield self.problem(path, message, f"The choices are {choices}.")

    def cycles(self) -> Iterator[Problem]:
        done: set[str] = set()
        for start in self.features:
            if start in done:
                continue
            trail: list[str] = []
            stack = [(start, 0)]
            while stack:
                name, index = stack.pop()
                if index == 0:
                    trail.append(name)
                requires = self.features[name].requires
                while index < len(requires) and (requires[index] in done or requires[index] not in self.features):
                    index += 1
                if index == len(requires):
                    done.add(name)
                    trail.pop()
                    continue
                stack.append((name, index + 1))
                target = requires[index]
                if target in trail:
                    loop = " → ".join([*trail[trail.index(target) :], target])
                    path = (*self.features[name].at, "requires", index)
                    yield self.problem(path, f"{target!r} closes a cycle: {loop}.", "Drop one of the requires in that cycle.")
                else:
                    stack.append((target, 0))


def _fields(item: str) -> list[str]:
    """The fields an ``item`` format string uses, in order, each once."""
    return list(dict.fromkeys(field for _, field, _, _ in string.Formatter().parse(item) if field))


def _closest(name: str, names: Collection[str], listing: str) -> str:
    """A fix suggesting the name closest to ``name``, or else ``listing`` filled with every name."""
    close = difflib.get_close_matches(name, names, n=1)
    return f"Did you mean {close[0]!r}?" if close else listing.format(", ".join(names))
