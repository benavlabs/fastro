"""Rendering the wiring a selection gets (spec 2.6 to 2.8)."""

from __future__ import annotations

from collections.abc import Mapping

from fastro.engine.blueprint import Blueprint
from fastro.engine.resolve import APP, Taken

CORE = "core"


def feature_indexes(catalog: Blueprint) -> dict[str, int]:
    """Each owner's ``feature_index`` (2.7): ``core`` is -1, then each feature in catalog order, and ``app`` last."""
    indexes = {CORE: -1}
    indexes.update({f"feature:{feature.name}": index for index, feature in enumerate(catalog.features)})
    indexes[APP] = len(catalog.features)
    return indexes


def ordered(catalog: Blueprint, found: Mapping[str, tuple[Taken, ...]]) -> dict[str, tuple[Taken, ...]]:
    """Each point's contributions sorted by ``(order, feature_index, position)``, keeping ties stable (2.7).

    A contribution with no ``order`` gets ``(feature_index + 1) * 100 + position``.
    """
    indexes = feature_indexes(catalog)

    def key(item: Taken) -> tuple[int, int, int]:
        index = indexes[item.owner]
        order = item.contribution.order
        return (order if order is not None else (index + 1) * 100 + item.position, index, item.position)

    return {point: tuple(sorted(items, key=key)) for point, items in found.items()}
