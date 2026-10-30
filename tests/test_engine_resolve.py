"""Resolving a selection against a catalog (A2): closure under ``requires``."""

from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.resolve import UnknownFeature, select

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini" / "blueprint.toml"


@pytest.fixture(scope="module")
def mini() -> Blueprint:
    return blueprint.load("blueprint.toml", MINI.read_text(), Version("0.1.0"))


@pytest.mark.parametrize(
    ("requested", "closed"),
    [
        ((), ()),
        (("accounts",), ("accounts",)),
        (("billing",), ("accounts", "billing")),
        (("admin", "tasks"), ("accounts", "tasks", "admin")),
        (("admin", "billing", "accounts"), ("accounts", "billing", "admin")),
        (("cache", "cache"), ("cache",)),
    ],
)
def test_the_selection_is_closed_under_requires_in_catalog_order(
    mini: Blueprint, requested: tuple[str, ...], closed: tuple[str, ...]
) -> None:
    assert select(mini, requested).features == closed


@pytest.mark.parametrize(
    ("preset", "closed"), [("minimal", ("accounts",)), ("full", ("accounts", "billing", "cache", "tasks", "admin"))]
)
def test_a_preset_selects_its_features(mini: Blueprint, preset: str, closed: tuple[str, ...]) -> None:
    assert select(mini, mini.presets[preset]).features == closed


def test_app_requires_are_part_of_the_closure(mini: Blueprint) -> None:
    selection = select(mini, ("cache",), app_requires=("admin",))

    assert selection.features == ("accounts", "cache", "admin")
    assert selection.chain("accounts") == ("app", "admin", "accounts")


@pytest.mark.parametrize(
    ("requested", "feature", "chain"),
    [
        (("billing",), "billing", ("billing",)),
        (("billing",), "accounts", ("billing", "accounts")),
        (("admin", "billing"), "accounts", ("billing", "accounts")),
        (("accounts", "billing"), "accounts", ("accounts",)),
    ],
)
def test_the_chain_says_who_pulled_a_feature_in(
    mini: Blueprint, requested: tuple[str, ...], feature: str, chain: tuple[str, ...]
) -> None:
    assert select(mini, requested).chain(feature) == chain


def test_a_feature_outside_the_selection_has_no_chain(mini: Blueprint) -> None:
    with pytest.raises(KeyError, match="'cache' isn't selected"):
        select(mini, ("billing",)).chain("cache")


@pytest.mark.parametrize(
    ("requested", "message"),
    [
        (("acounts",), "'acounts' isn't a feature of this catalog. Did you mean 'accounts'?"),
        (("payments",), "'payments' isn't a feature of this catalog. The features are accounts, billing, cache, tasks, admin."),
    ],
)
def test_an_unknown_feature_is_refused_with_the_closest_name(mini: Blueprint, requested: tuple[str, ...], message: str) -> None:
    with pytest.raises(UnknownFeature) as caught:
        select(mini, requested)

    assert str(caught.value) == message


def test_an_unknown_app_requirement_is_refused_too(mini: Blueprint) -> None:
    with pytest.raises(UnknownFeature, match="'jwt' isn't a feature of this catalog"):
        select(mini, (), app_requires=("jwt",))


DEEP = """\
[blueprint]
name = "deep"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[features.base]
summary = "Base"

[features.middle]
summary = "Middle"
requires = ["base"]

[features.top]
summary = "Top"
requires = ["middle"]

[features.side]
summary = "Side"
requires = ["base"]
"""


def test_a_chain_follows_requires_all_the_way_down() -> None:
    deep = blueprint.load("blueprint.toml", DEEP, Version("0.1.0"))
    selection = select(deep, ("top", "side"))

    assert selection.features == ("base", "middle", "top", "side")
    assert (selection.chain("middle"), selection.chain("base")) == (("top", "middle"), ("side", "base"))


def test_requires_are_followed_past_the_first_level() -> None:
    deep = blueprint.load("blueprint.toml", DEEP, Version("0.1.0"))
    selection = select(deep, ("top",))

    assert (selection.features, selection.chain("base")) == (("base", "middle", "top"), ("top", "middle", "base"))


def test_a_feature_asked_for_directly_is_credited_to_the_request_not_to_app(mini: Blueprint) -> None:
    assert select(mini, ("accounts",), app_requires=("accounts",)).chain("accounts") == ("accounts",)


@pytest.mark.parametrize("app_requires", [("billing", "admin"), ("admin", "billing")])
def test_app_requires_are_credited_in_catalog_order_whatever_order_they_are_listed_in(
    mini: Blueprint, app_requires: tuple[str, ...]
) -> None:
    assert select(mini, (), app_requires=app_requires).chain("accounts") == ("app", "billing", "accounts")
