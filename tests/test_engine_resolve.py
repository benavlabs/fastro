"""Resolving a selection against a catalog: closure under ``requires``, options, conditions, the contributions it
takes, and who owns each file."""

from collections.abc import Mapping
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint, conditions, project
from fastro.engine.blueprint import Blueprint
from fastro.engine.resolve import (
    NoContribution,
    OptionError,
    Ownership,
    SelectionError,
    Taken,
    TooManyContributions,
    UnknownFeature,
    check_one_points,
    check_value_slots,
    select,
    taken,
)
from fastro.engine.spec import RepoPath

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


@pytest.mark.parametrize(
    ("requested", "options", "chosen"),
    [
        (("billing",), {}, {("billing", "provider"): "stripe"}),
        (("billing",), {("billing", "provider"): "paddle"}, {("billing", "provider"): "paddle"}),
        (("billing",), {("cache", "backend"): "redis"}, {("billing", "provider"): "stripe"}),
        (("accounts",), {}, {}),
        (
            ("accounts", "billing", "cache", "tasks", "admin"),
            {("cache", "backend"): "redis"},
            {("billing", "provider"): "stripe", ("cache", "backend"): "redis"},
        ),
    ],
)
def test_each_selected_feature_gets_its_options_with_defaults_and_unselected_ones_are_dropped(
    mini: Blueprint, requested: tuple[str, ...], options: dict[tuple[str, str], str], chosen: dict[tuple[str, str], str]
) -> None:
    assert select(mini, requested, options=options).options == chosen


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({("billing", "provider"): "square"}, "'square' isn't a choice of billing.provider. The choices are stripe, paddle."),
        (
            {("billing", "currency"): "eur"},
            "'billing.currency' isn't an option of billing. The options of billing are provider.",
        ),
        ({("billing", "provder"): "paddle"}, "'billing.provder' isn't an option of billing. Did you mean 'provider'?"),
        ({("accounts", "mode"): "fast"}, "'accounts.mode' isn't an option of accounts. accounts has no options."),
        ({("cache", "backend"): "valkey"}, "'valkey' isn't a choice of cache.backend. The choices are memory, redis."),
        ({("tasks", "queue"): "x"}, "'tasks.queue' isn't an option of tasks. tasks has no options."),
    ],
)
def test_an_option_a_selected_feature_doesnt_have_is_refused(
    mini: Blueprint, options: dict[tuple[str, str], str], message: str
) -> None:
    with pytest.raises(OptionError) as caught:
        select(mini, ("billing",), options=options)

    assert str(caught.value) == message
    assert isinstance(caught.value, SelectionError)


def test_an_option_of_a_feature_the_catalog_doesnt_declare_is_refused(mini: Blueprint) -> None:
    with pytest.raises(UnknownFeature, match="'payments' isn't a feature of this catalog"):
        select(mini, ("billing",), options={("payments", "provider"): "stripe"})


@pytest.mark.parametrize(
    ("condition", "holds"),
    [
        ("billing", True),
        ("cache", False),
        ("not cache", True),
        ("billing and accounts", True),
        ("billing and cache", False),
        ("cache or billing", True),
        ("billing.provider=paddle", True),
        ("billing.provider=stripe", False),
        ("cache.backend=memory", False),
        ("not (cache or billing.provider=stripe)", True),
    ],
)
def test_a_condition_holds_against_the_selection_and_its_options(mini: Blueprint, condition: str, holds: bool) -> None:
    selection = select(mini, ("billing",), options={("billing", "provider"): "paddle"})

    assert selection.holds(conditions.parse(condition)) is holds


def test_a_condition_keeps_the_text_the_catalog_wrote(mini: Blueprint) -> None:
    billing = next(feature for feature in mini.features if feature.name == "billing")
    files = {file.path.text: file.condition_text for file in mini.wiring}

    assert billing.when[0].condition_text == "billing.provider=paddle"
    assert (files["frontend/menu.ts"], files["backend/src/wiring/app.py"]) == ("admin", "")


@pytest.mark.parametrize(
    ("path", "origin", "when"),
    [
        ("backend/src/modules/billing/routes.py", "feature:billing", None),
        ("backend/src/modules/billing/paddle.py", "feature:billing", 0),
        ("backend/tests/test_accounts.py", "feature:accounts", None),
        ("backend/src/wiring/app.py", "core", None),
        ("backend/src/app.py", "core", None),
        ("README.md", "core", None),
    ],
)
def test_a_file_belongs_to_the_longest_path_that_covers_it(mini: Blueprint, path: str, origin: str, when: int | None) -> None:
    owner = Ownership(mini).of(RepoPath(path))

    assert (owner.origin, owner.when) == (origin, when)


def test_a_when_path_belongs_to_its_when_whatever_the_options(mini: Blueprint) -> None:
    owner = Ownership(mini).of(RepoPath("backend/src/modules/billing/paddle.py"))

    assert (owner.feature, owner.when, owner.at) == ("billing", 0, ("features", "billing", "when", 0, "paths", 0))


@pytest.mark.parametrize(
    ("app_paths", "path", "origin"),
    [
        (("backend/src/custom/",), "backend/src/custom/views.py", "app"),
        (("backend/src/modules/billing/custom.py",), "backend/src/modules/billing/custom.py", "app"),
        (("backend/src/modules/billing/custom.py",), "backend/src/modules/billing/routes.py", "feature:billing"),
        (("backend/src/",), "backend/src/app.py", "app"),
        (("backend/src/",), "backend/src/modules/billing/routes.py", "feature:billing"),
        (("backend/src",), "backend/src/app.py", "core"),
    ],
)
def test_app_paths_take_part_in_the_contest(mini: Blueprint, app_paths: tuple[str, ...], path: str, origin: str) -> None:
    assert Ownership(mini, tuple(RepoPath(text) for text in app_paths)).of(RepoPath(path)).origin == origin


def test_an_owner_names_where_its_path_is_declared(mini: Blueprint) -> None:
    ownership = Ownership(mini, (RepoPath("backend/src/custom/"),))

    assert ownership.of(RepoPath("backend/tests/test_accounts.py")).at == ("features", "accounts", "paths", 1)
    assert ownership.of(RepoPath("backend/src/custom/a.py")).at == ("app", "paths", 0)
    assert ownership.of(RepoPath("backend/src/app.py")).at == ()


def _points(found: Mapping[str, tuple[Taken, ...]]) -> dict[str, list[tuple[str, str]]]:
    return {point: [(item.owner, str(item.contribution.target)) for item in items] for point, items in found.items()}


def test_the_full_preset_takes_each_contribution_whose_conditions_hold(mini: Blueprint) -> None:
    found = taken(mini, select(mini, mini.presets["full"]))

    assert _points(found) == {
        "health_checks": [("core", "src.health:database")],
        "routers": [
            ("feature:accounts", "src.modules.accounts.routes:router as accounts_router"),
            ("feature:billing", "src.modules.billing.routes:router as billing_router"),
        ],
        "email_sender": [("feature:accounts", "src.modules.accounts.email:send_queued")],
        "settings": [("feature:cache", "src.modules.cache.settings:CacheSettings")],
        "task_modules": [("feature:tasks", "src.modules.tasks.jobs")],
        "admin_site": [("feature:admin", "src.modules.admin.site:site")],
        "menu": [("feature:admin", "{'href': '/admin', 'label': 'Admin'}")],
    }
    check_one_points(mini, found)


@pytest.mark.parametrize(("requested", "sender"), [(("accounts",), "send_now"), (("accounts", "tasks"), "send_queued")])
def test_one_owner_feeding_a_one_point_under_exclusive_conditions_gives_it_one(
    mini: Blueprint, requested: tuple[str, ...], sender: str
) -> None:
    found = taken(mini, select(mini, requested))
    check_one_points(mini, found)

    assert [str(item.contribution.target) for item in found["email_sender"]] == [f"src.modules.accounts.email:{sender}"]


def test_app_contributions_are_taken_when_their_if_holds(mini: Blueprint) -> None:
    app = project.load(
        "fastro.toml",
        'blueprint = { name = "mini", version = "0.1.0" }\nfeatures = []\n[project]\nname = "a"\n'
        '[app]\ncontributes = { routers = [{ ref = "src.app:r", prefix = "/a", if = "billing" }, '
        '{ ref = "src.app:s", prefix = "/s", if = "cache" }] }\n',
    ).app.contributes

    found = taken(mini, select(mini, ("billing",)), app)

    assert [item.owner for item in found["routers"]] == ["feature:accounts", "feature:billing", "app"]


CLASH = """\
[blueprint]
name = "clash"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[points.sender]
cardinality = "one"

[[wiring]]
path = "backend/src/wiring.py"
doc = "Wiring."

[[wiring.slots]]
kind = "value"
name = "SENDER"
point = "sender"
annotation = "object"
fallback = "None"

[features.mail]
summary = "Mail"

[features.mail.contributes]
sender = [{ ref = "src.mail:smtp", if = "queue" }]

[[features.mail.when]]
if = "queue"
contributes = { sender = ["src.mail:queued"] }

[features.queue]
summary = "Queue"
"""


def test_a_one_point_given_two_contributions_by_the_selection_is_refused() -> None:
    clash = blueprint.load("blueprint.toml", CLASH, Version("0.1.0"))
    found = taken(clash, select(clash, ("mail", "queue")))

    with pytest.raises(TooManyContributions) as caught:
        check_one_points(clash, found)

    assert str(caught.value) == (
        "'sender' is a one point, and this selection gives it 2 contributions: "
        "feature:mail at features.mail.contributes.sender[0] and "
        "feature:mail at features.mail.when[0].contributes.sender[0]. "
        "Drop one of them, or give them conditions that exclude each other."
    )
    assert isinstance(caught.value, SelectionError)


def test_a_feature_and_app_giving_a_one_point_a_contribution_each_is_refused() -> None:
    once = CLASH.replace('[[features.mail.when]]\nif = "queue"\ncontributes = { sender = ["src.mail:queued"] }\n', "")
    clash = blueprint.load("blueprint.toml", once, Version("0.1.0"))
    app = project.load(
        "fastro.toml",
        'blueprint = { name = "clash", version = "0.1.0" }\nfeatures = []\n[project]\nname = "a"\n'
        '[app]\ncontributes = { sender = ["src.app:mine"] }\n',
    ).app.contributes

    with pytest.raises(TooManyContributions) as caught:
        check_one_points(clash, taken(clash, select(clash, ("mail", "queue")), app))

    assert "2 contributions: feature:mail at features.mail.contributes.sender[0] and app at app.contributes.sender[0]." in str(
        caught.value
    )


def test_a_one_point_given_one_contribution_or_none_passes() -> None:
    clash = blueprint.load("blueprint.toml", CLASH, Version("0.1.0"))

    for requested in ((), ("mail",), ("queue",)):
        check_one_points(clash, taken(clash, select(clash, requested)))


EMPTY = CLASH.replace('fallback = "None"\n', "").replace(
    '[[features.mail.when]]\nif = "queue"\ncontributes = { sender = ["src.mail:queued"] }\n', ""
)


@pytest.mark.parametrize("requested", [(), ("mail",), ("queue",)])
def test_a_value_slot_whose_point_the_selection_leaves_empty_needs_a_fallback(requested: tuple[str, ...]) -> None:
    empty = blueprint.load("blueprint.toml", EMPTY, Version("0.1.0"))

    with pytest.raises(NoContribution) as caught:
        check_value_slots(empty, taken(empty, select(empty, requested)))

    assert str(caught.value) == (
        "SENDER at wiring[0].slots[0] in backend/src/wiring.py is a value slot with no fallback, and this selection "
        "gives its point, 'sender', no contribution. Select a feature that contributes to it, or give the slot a fallback."
    )
    assert isinstance(caught.value, SelectionError)


def test_a_value_slot_with_a_contribution_or_a_fallback_passes() -> None:
    empty = blueprint.load("blueprint.toml", EMPTY, Version("0.1.0"))
    clash = blueprint.load("blueprint.toml", CLASH, Version("0.1.0"))

    check_value_slots(empty, taken(empty, select(empty, ("mail", "queue"))))
    for requested in ((), ("mail",), ("queue",)):
        check_value_slots(clash, taken(clash, select(clash, requested)))


def test_mini_s_value_slots_pass_under_every_preset(mini: Blueprint) -> None:
    for features in mini.presets.values():
        check_value_slots(mini, taken(mini, select(mini, features)))
