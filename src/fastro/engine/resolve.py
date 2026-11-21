"""Resolving what a project selects against its catalog: the closure under ``requires``, the contributions it
takes, and who owns a file."""

from __future__ import annotations

import difflib
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fastro.engine import conditions
from fastro.engine.blueprint import Blueprint, Contribution, Contributions, Feature
from fastro.engine.conditions import Condition
from fastro.engine.positions import KeyPath
from fastro.engine.problems import key_name
from fastro.engine.spec import RepoPath

APP = "app"
FALLBACK_KINDS = ("value", "reexport")


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


class TooManyContributions(SelectionError):
    """A ``one`` point the selection gives more than one contribution (spec 2.3)."""


class NoContribution(SelectionError):
    """A ``value`` or ``reexport`` slot with no fallback whose point the selection gives no contribution (spec 2.6)."""


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


@dataclass(frozen=True)
class Taken:
    """A contribution the selection takes, who made it (``core``, ``feature:<name>`` or ``app``), and its position.

    ``position`` is its index in its owner's whole list for that point, the feature's own contributions first and
    then each ``when``'s, counting the ones the selection doesn't take, so it never depends on the selection (2.7).
    """

    contribution: Contribution
    owner: str
    position: int


def taken(catalog: Blueprint, selection: Selection, app: Contributions | None = None) -> dict[str, tuple[Taken, ...]]:
    """Every contribution ``selection`` takes, by point, in declaration order.

    That's ``[core]``'s, each selected feature's own and those of its ``when`` tables whose ``if`` holds, then
    the project's ``[app]``. Each counts only when its own ``if`` holds too. Sorting them is rendering's job (2.7).
    """
    groups: list[tuple[str, Contributions, bool]] = [("core", catalog.core, True)]
    for feature in catalog.features:
        if feature.name in selection.pulled_by:
            owner = f"feature:{feature.name}"
            groups.append((owner, feature.contributes, True))
            groups += [(owner, when.contributes, selection.holds(when.condition)) for when in feature.when]
    groups.append((APP, app or {}, True))
    found: dict[str, list[Taken]] = {}
    positions: dict[tuple[str, str], int] = {}
    for owner, contributes, active in groups:
        for point, items in contributes.items():
            for item in items:
                position = positions.get((owner, point), 0)
                positions[(owner, point)] = position + 1
                if active and (item.condition is None or selection.holds(item.condition)):
                    found.setdefault(point, []).append(Taken(item, owner, position))
    return {point: tuple(items) for point, items in found.items()}


def _place(catalog: Blueprint, item: Taken) -> str:
    """How a message names where ``item`` is written: in ``fastro.toml`` for the app's, else in ``blueprint.toml``."""
    return key_name(item.contribution.at) if item.owner == APP else catalog.document.key(item.contribution.at)


def check_one_points(catalog: Blueprint, found: Mapping[str, tuple[Taken, ...]]) -> None:
    """Refuse a ``one`` point that ``found`` gives more than one contribution (spec 2.3)."""
    for point, items in found.items():
        if point in catalog.points and catalog.points[point].cardinality == "one" and len(items) > 1:
            listed = [f"{item.owner} at {_place(catalog, item)}" for item in items]
            given = f"{', '.join(listed[:-1])} and {listed[-1]}"
            raise TooManyContributions(
                f"{point!r} is a one point, and this selection gives it {len(items)} contributions: {given}. "
                "Drop one of them, or give them conditions that exclude each other."
            )


def check_fallback_slots(catalog: Blueprint, selection: Selection, found: Mapping[str, tuple[Taken, ...]]) -> None:
    """Refuse a ``value`` or ``reexport`` slot with no fallback, in a file ``selection`` renders, whose point ``found``
    gives no contribution (spec 2.6)."""
    for i, file in enumerate(catalog.wiring):
        if file.condition is not None and not selection.holds(file.condition):
            continue
        for j, slot in enumerate(file.slots):
            if slot.kind in FALLBACK_KINDS and slot.fallback is None and not found.get(slot.point):
                raise NoContribution(
                    f"{catalog.document.key(('wiring', i, 'slots', j))} is a {slot.kind} slot "
                    f"with no fallback, and this selection gives its point, {slot.point!r}, no contribution. "
                    "Select a feature that contributes to it, or give the slot a fallback."
                )


@dataclass(frozen=True)
class Owner:
    """Who a file belongs to (spec 1.1): a feature, through its own ``paths`` or a ``when``, ``[app]``, or else core.

    ``at`` is where the path that won is declared, and is empty for core.
    """

    feature: str = ""
    when: int | None = None
    app: bool = False
    at: KeyPath = ()

    @property
    def origin(self) -> str:
        """The plan's ``origin`` for the file (spec 7): ``feature:<name>``, ``app`` or ``core``."""
        if self.app:
            return APP
        return f"feature:{self.feature}" if self.feature else "core"


class Ownership:
    """Who owns each file: the declaration with the longest path that takes it in (spec 1.1).

    Conditions play no part, so a ``when`` path owns its files whether or not its ``if`` holds.
    """

    def __init__(self, catalog: Blueprint, app_paths: Iterable[RepoPath] = ()) -> None:
        self.declarations: list[tuple[RepoPath, Owner]] = []
        for feature in catalog.features:
            for index, path in enumerate(feature.paths):
                self.declarations.append((path, Owner(feature.name, at=(*feature.at, "paths", index))))
            for number, when in enumerate(feature.when):
                for index, path in enumerate(when.paths):
                    at = (*feature.at, "when", number, "paths", index)
                    self.declarations.append((path, Owner(feature.name, number, at=at)))
        for index, path in enumerate(app_paths):
            self.declarations.append((path, Owner(app=True, at=(APP, "paths", index))))

    def of(self, path: RepoPath) -> Owner:
        """The owner of the file at ``path``, or core when no declared path takes it in."""
        best: tuple[RepoPath, Owner] | None = None
        for declared, owner in self.declarations:
            if declared.covers(path.text) and (best is None or len(declared.text) > len(best[0].text)):
                best = (declared, owner)
        return best[1] if best is not None else Owner()
