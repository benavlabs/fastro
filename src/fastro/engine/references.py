"""Names in a loaded ``blueprint.toml`` that refer to another section, checked once every key reads cleanly."""

from __future__ import annotations

import difflib
from collections.abc import Collection, Iterator
from typing import TYPE_CHECKING

from fastro.engine import conditions
from fastro.engine.conditions import Condition
from fastro.engine.positions import KeyPath
from fastro.engine.problems import Problem

if TYPE_CHECKING:
    from fastro.engine.blueprint import Blueprint, Contributions


def problems(catalog: Blueprint) -> list[Problem]:
    """Every name in ``catalog`` that refers to a feature, option or choice it doesn't declare, or a ``requires`` cycle."""
    return list(_Checker(catalog).run())


class _Checker:
    def __init__(self, catalog: Blueprint) -> None:
        self.catalog = catalog
        self.features = {feature.name: feature for feature in catalog.features}

    def problem(self, path: KeyPath, message: str, fix: str) -> Problem:
        return self.catalog.document.problem(path, message, fix)

    def run(self) -> Iterator[Problem]:
        catalog = self.catalog
        for feature in catalog.features:
            for index, name in enumerate(feature.requires):
                yield from self.feature((*feature.at, "requires", index), name)
            yield from self.contributions((*feature.at, "contributes"), feature.contributes)
            for index, when in enumerate(feature.when):
                yield from self.condition((*feature.at, "when", index, "if"), when.condition)
                yield from self.contributions((*feature.at, "when", index, "contributes"), when.contributes)
        yield from self.cycles()
        yield from self.contributions(("core", "contributes"), catalog.core)
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
        for generator in catalog.generators.values():
            if generator.register is not None:
                for at, condition in generator.register.conditions.items():
                    yield from self.condition((*at, "if"), condition)
        if catalog.plugin_api is not None:
            for name in catalog.plugin_api.public_with:
                yield from self.feature((*catalog.plugin_api.at, "public_with", name), name)

    def feature(self, path: KeyPath, name: str) -> Iterator[Problem]:
        if name not in self.features:
            listing = "The features are {}." if self.features else "This catalog declares no features yet."
            fix = _closest(name, self.features, listing)
            yield self.problem(path, f"{name!r} isn't a feature of this catalog.", fix)

    def contributions(self, path: KeyPath, contributes: Contributions) -> Iterator[Problem]:
        for point, items in contributes.items():
            for index, item in enumerate(items):
                yield from self.condition((*path, point, index, "if"), item.condition)

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


def _closest(name: str, names: Collection[str], listing: str) -> str:
    """A fix suggesting the name closest to ``name``, or else ``listing`` filled with every name."""
    close = difflib.get_close_matches(name, names, n=1)
    return f"Did you mean {close[0]!r}?" if close else listing.format(", ".join(names))
