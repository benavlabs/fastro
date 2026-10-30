"""Resolving what a project selects against its catalog: the closure under ``requires``."""

from __future__ import annotations

import difflib
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fastro.engine import conditions
from fastro.engine.blueprint import Blueprint, Feature
from fastro.engine.conditions import Condition

APP = "app"


class SelectionError(ValueError):
    """A selection the catalog can't resolve: an unknown feature, or an option a selected feature doesn't have."""


class UnknownFeature(SelectionError):
    """A requested feature the catalog doesn't declare."""

    def __init__(self, name: str, known: Iterable[str]) -> None:
        names = list(known)
        close = difflib.get_close_matches(name, names, n=1)
        hint = f"Did you mean {close[0]!r}?" if close else f"The features are {', '.join(names)}."
        super().__init__(f"{name!r} isn't a feature of this catalog. {hint}")


class OptionError(SelectionError):
    """An option a selected feature doesn't declare, or a value that isn't one of its choices."""


@dataclass(frozen=True)
class Selection:
    """The features a project gets, closed under ``requires`` and in catalog order.

    ``pulled_by`` maps each feature to the one that first required it, ``app`` for ``[app].requires``,
    or ``None`` when it was asked for directly.
    """

    features: tuple[str, ...]
    pulled_by: Mapping[str, str | None]
    options: Mapping[tuple[str, str], str]

    def holds(self, condition: Condition) -> bool:
        """Whether ``condition`` is true for this selection and its options (spec 1.3)."""
        if isinstance(condition, conditions.Feature):
            return condition.name in self.pulled_by
        if isinstance(condition, conditions.Option):
            return self.options.get((condition.feature, condition.option)) == condition.value
        if isinstance(condition, conditions.Not):
            return not self.holds(condition.operand)
        if isinstance(condition, conditions.And):
            return all(self.holds(operand) for operand in condition.operands)
        return any(self.holds(operand) for operand in condition.operands)

    def chain(self, name: str) -> tuple[str, ...]:
        """Who pulled ``name`` in, from what was asked for down to ``name``: ``("billing", "accounts")``."""
        if name not in self.pulled_by:
            raise KeyError(f"{name!r} isn't selected.")
        chain = [name]
        while (parent := self.pulled_by.get(chain[-1])) is not None:
            chain.append(parent)
        return tuple(reversed(chain))


def select(
    catalog: Blueprint,
    features: Iterable[str],
    app_requires: Iterable[str] = (),
    options: Mapping[tuple[str, str], str] | None = None,
) -> Selection:
    """The closure of ``features`` and the project's ``app_requires`` under ``requires`` (spec 2.4), with options.

    Every entry in ``options`` is checked against the catalog. Then each selected feature gets every option
    it declares, the value in ``options`` or else its default, and entries for a feature the closure doesn't
    select are dropped (spec 3).

    A feature reached two ways is credited along the shortest chain, and among equally short ones to what was
    asked for first in catalog order, with ``[app].requires`` after it. So the result doesn't depend on the
    order of the request.
    """
    declared = {feature.name: feature for feature in catalog.features}
    order = {name: index for index, name in enumerate(declared)}
    requested, required = list(features), list(app_requires)
    for name in (*requested, *required):
        if name not in declared:
            raise UnknownFeature(name, declared)
    pulled_by: dict[str, str | None] = {name: None for name in sorted(set(requested), key=order.__getitem__)}
    for name in sorted(set(required), key=order.__getitem__):
        pulled_by.setdefault(name, APP)
    queue = deque(pulled_by)
    while queue:
        current = queue.popleft()
        for name in declared[current].requires:
            if name not in pulled_by:
                pulled_by[name] = current
                queue.append(name)
    chosen = dict(options or {})
    for feature, _ in chosen:
        if feature not in declared:
            raise UnknownFeature(feature, declared)
    selected = tuple(sorted(pulled_by, key=order.__getitem__))
    return Selection(selected, pulled_by, _options(declared, selected, chosen))


def _options(
    declared: Mapping[str, Feature], selected: tuple[str, ...], chosen: Mapping[tuple[str, str], str]
) -> dict[tuple[str, str], str]:
    for (feature, option), value in chosen.items():
        options = declared[feature].options
        if option not in options:
            close = difflib.get_close_matches(option, options, n=1)
            listing = f"The options of {feature} are {', '.join(options)}." if options else f"{feature} has no options."
            hint = f"Did you mean {close[0]!r}?" if close else listing
            raise OptionError(f"'{feature}.{option}' isn't an option of {feature}. {hint}")
        if value not in options[option].choices:
            choices = ", ".join(options[option].choices)
            raise OptionError(f"{value!r} isn't a choice of {feature}.{option}. The choices are {choices}.")
    return {
        (feature, name): chosen.get((feature, name), option.default)
        for feature in selected
        for name, option in declared[feature].options.items()
    }
