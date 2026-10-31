"""Rendering the wiring (spec 2.6 to 2.8): first, the order contributions take in a slot (2.7)."""

from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint, project
from fastro.engine.blueprint import Blueprint, Contributions
from fastro.engine.render import feature_indexes, ordered
from fastro.engine.resolve import select, taken

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini" / "blueprint.toml"

ORDERS = """\
[blueprint]
name = "orders"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[points.routers]
cardinality = "many"

[[wiring]]
path = "backend/src/wiring.py"
doc = "Wiring."

[[wiring.slots]]
kind = "tuple"
name = "ROUTERS"
point = "routers"
annotation = "tuple[object, ...]"

[core.contributes]
routers = ["src.core:health"]

[features.a]
summary = "A"

[features.a.contributes]
routers = ["src.a:first", { ref = "src.a:skipped", if = "c" }, "src.a:second"]

[[features.a.when]]
if = "b"
contributes = { routers = ["src.a:with_b"] }

[features.b]
summary = "B"

[features.b.contributes]
routers = [{ ref = "src.b:early", order = 50 }, "src.b:late"]

[features.c]
summary = "C"
"""


def _catalog(text: str = ORDERS) -> Blueprint:
    return blueprint.load("blueprint.toml", text, Version("0.1.0"))


def _routers(catalog: Blueprint, features: tuple[str, ...], app: str = "") -> list[str]:
    contributes: Contributions = {}
    if app:
        contributes = project.load(
            "fastro.toml",
            f'blueprint = {{ name = "orders", version = "0.1.0" }}\nfeatures = []\n[project]\nname = "p"\n{app}',
        ).app.contributes
    found = ordered(catalog, taken(catalog, select(catalog, features), contributes))
    return [str(item.contribution.target) for item in found["routers"]]


def test_mini_mounts_billing_before_accounts_by_its_explicit_order() -> None:
    mini = blueprint.load("blueprint.toml", MINI.read_text(), Version("0.1.0"))
    found = ordered(mini, taken(mini, select(mini, mini.presets["full"])))

    assert [item.owner for item in found["routers"]] == ["feature:billing", "feature:accounts"]


def test_core_comes_first_then_features_in_catalog_order_then_when_contributions_after_their_own() -> None:
    assert _routers(_catalog(), ("a", "b")) == [
        "src.core:health",
        "src.b:early",
        "src.a:first",
        "src.a:second",
        "src.a:with_b",
        "src.b:late",
    ]


def test_app_comes_last() -> None:
    routers = _routers(_catalog(), ("a",), app='[app]\ncontributes = { routers = ["src.app:mine"] }\n')

    assert routers == ["src.core:health", "src.a:first", "src.a:second", "src.app:mine"]


def test_an_explicit_order_below_100_goes_before_every_default() -> None:
    routers = _routers(_catalog(), ("a",), app='[app]\ncontributes = { routers = [{ ref = "src.app:first", order = 1 }] }\n')

    assert routers[:2] == ["src.core:health", "src.app:first"]


def test_a_position_counts_the_features_whole_list_whatever_is_selected() -> None:
    catalog = _catalog(ORDERS.replace('{ ref = "src.b:early", order = 50 }', '{ ref = "src.b:early", order = 101 }'))

    assert _routers(catalog, ("a", "b")) == [
        "src.core:health",
        "src.a:first",
        "src.b:early",
        "src.a:second",
        "src.a:with_b",
        "src.b:late",
    ]


def test_equal_orders_are_broken_by_feature_index_then_position() -> None:
    catalog = _catalog(
        ORDERS.replace('{ ref = "src.b:early", order = 50 }', '{ ref = "src.b:early", order = 7 }').replace(
            '"src.a:second"]', '{ ref = "src.a:second", order = 7 }]'
        )
    )

    assert _routers(catalog, ("a", "b"))[:3] == ["src.core:health", "src.a:second", "src.b:early"]


def test_a_when_that_fails_still_counts_toward_the_positions_after_it() -> None:
    catalog = _catalog(
        ORDERS.replace(
            '[[features.a.when]]\nif = "b"\ncontributes = { routers = ["src.a:with_b"] }\n',
            '[[features.a.when]]\nif = "c"\ncontributes = { routers = ["src.a:with_c"] }\n'
            '[[features.a.when]]\nif = "b"\ncontributes = { routers = ["src.a:with_b"] }\n',
        ).replace('{ ref = "src.b:early", order = 50 }', '{ ref = "src.b:early", order = 103 }')
    )

    assert _routers(catalog, ("a", "b")) == [
        "src.core:health",
        "src.a:first",
        "src.a:second",
        "src.b:early",
        "src.a:with_b",
        "src.b:late",
    ]


def test_the_feature_index_is_minus_one_for_core_then_each_feature_then_app() -> None:
    assert feature_indexes(_catalog()) == {"core": -1, "feature:a": 0, "feature:b": 1, "feature:c": 2, "app": 3}


@pytest.mark.parametrize("features", [("a", "b"), ("b", "a")])
def test_the_order_doesnt_depend_on_the_order_features_were_asked_for(features: tuple[str, ...]) -> None:
    assert _routers(_catalog(), features)[0:2] == ["src.core:health", "src.b:early"]
