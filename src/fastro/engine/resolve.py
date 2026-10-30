"""Resolving what a project selects against its catalog: the closure under ``requires``."""

from __future__ import annotations

import difflib
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fastro.engine.blueprint import Blueprint

APP = "app"


class UnknownFeature(ValueError):
    """A requested feature the catalog doesn't declare."""

    def __init__(self, name: str, known: Iterable[str]) -> None:
        names = list(known)
        close = difflib.get_close_matches(name, names, n=1)
        hint = f"Did you mean {close[0]!r}?" if close else f"The features are {', '.join(names)}."
        super().__init__(f"{name!r} isn't a feature of this catalog. {hint}")


@dataclass(frozen=True)
class Selection:
    """The features a project gets, closed under ``requires`` and in catalog order.

    ``pulled_by`` maps each feature to the one that first required it, ``app`` for ``[app].requires``,
    or ``None`` when it was asked for directly.
    """

    features: tuple[str, ...]
    pulled_by: Mapping[str, str | None]

    def chain(self, name: str) -> tuple[str, ...]:
        """Who pulled ``name`` in, from what was asked for down to ``name``: ``("billing", "accounts")``."""
        if name not in self.pulled_by:
            raise KeyError(f"{name!r} isn't selected.")
        chain = [name]
        while (parent := self.pulled_by.get(chain[-1])) is not None:
            chain.append(parent)
        return tuple(reversed(chain))


def select(catalog: Blueprint, features: Iterable[str], app_requires: Iterable[str] = ()) -> Selection:
    """The closure of ``features`` and the project's ``app_requires`` under ``requires`` (spec 2.4).

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
    return Selection(tuple(sorted(pulled_by, key=order.__getitem__)), pulled_by)
