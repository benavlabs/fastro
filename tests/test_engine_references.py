"""Names that refer to another section of ``blueprint.toml`` (spec 1.2, 1.3, 2.2, 2.4 and 2.11)."""

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint, load
from fastro.engine.problems import CatalogError

MINIMAL = """\
[blueprint]
name = "mini"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"
"""

FEATURES = """\
[points.routers]
cardinality = "many"

[features.accounts]
summary = "Users and login"

[features.taskiq]
summary = "Background tasks"
requires = ["accounts"]

[features.taskiq.options.broker]
choices = ["redis", "rabbitmq"]
default = "redis"
"""

EXTENDS = 'extends = { name = "fastro", version = "0.21.0" }\n'

ROUTERS_SLOT = """
[[wiring]]
path = "backend/src/routers.py"
doc = "Routers."

[[wiring.slots]]
kind = "tuple"
name = "ROUTERS"
point = "routers"
annotation = "tuple[object, ...]"
"""


def _load(text: str) -> Blueprint:
    return load("blueprint.toml", text, Version("0.1.0"))


def _problems(text: str) -> list[str]:
    with pytest.raises(CatalogError) as caught:
        _load(text)
    return [str(problem) for problem in caught.value.problems]


def test_names_that_all_exist_load() -> None:
    text = (
        MINIMAL
        + FEATURES
        + '[[features.accounts.when]]\nif = "taskiq.broker=rabbitmq and not taskiq"\n'
        + '[features.accounts.contributes]\nrouters = [{ ref = "m:r", if = "(accounts or taskiq)" }]\n'
        + '[features.taskiq.contributes]\nrouters = ["m:t"]\n'
        + '[core.contributes]\nrouters = [{ ref = "m:c", if = "taskiq" }]\n'
        + '[presets]\nminimal = ["accounts", "taskiq"]\n'
        + '[verify.sets]\nqueues = ["taskiq"]\n'
        + '[[verify.checks]]\nname = "q"\nrun = ["true"]\nif = "taskiq.broker=redis"\n'
        + '[[wiring]]\npath = "backend/src/w.py"\ndoc = "W."\nif = "accounts"\n'
        + '[[wiring.slots]]\nkind = "raw"\ncode = "X = 1"\n'
        + '[plugin_api]\nversion = 1\npublic_with = { taskiq = ["src.tasks"] }\n'
        + '[generators.deploy]\ntemplates = "d"\n'
        + '[generators.module]\ntemplates = "t"\n'
        + '[generators.module.register]\ncontributes = { routers = ["m:r", { ref = "m:a", if = "taskiq" }] }\n'
    )

    assert [feature.name for feature in _load(text + ROUTERS_SLOT).features] == ["accounts", "taskiq"]


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (
            FEATURES.replace('requires = ["accounts"]', 'requires = ["acounts"]'),
            "14: features.taskiq.requires[0]: 'acounts' isn't a feature of this catalog.\n  Did you mean 'accounts'?",
        ),
        (
            FEATURES.replace('requires = ["accounts"]', 'requires = ["queue"]'),
            "14: features.taskiq.requires[0]: 'queue' isn't a feature of this catalog.\n  The features are accounts, taskiq.",
        ),
        (
            FEATURES.replace('requires = ["accounts"]', 'requires = ["taskiq"]'),
            "14: features.taskiq.requires[0]: 'taskiq' closes a cycle: taskiq → taskiq.\n"
            "  Drop one of the requires in that cycle.",
        ),
        (
            FEATURES.replace('summary = "Users and login"', 'summary = "Users and login"\nrequires = ["taskiq"]'),
            "15: features.taskiq.requires[0]: 'accounts' closes a cycle: accounts → taskiq → accounts.\n"
            "  Drop one of the requires in that cycle.",
        ),
        (
            FEATURES
            + '[features.billing]\nsummary = "Billing"\nrequires = ["tiers"]\n'
            + '[features.tiers]\nsummary = "Tiers"\nrequires = ["webhooks"]\n'
            + '[features.webhooks]\nsummary = "Webhooks"\nrequires = ["tiers"]\n',
            "27: features.webhooks.requires[0]: 'tiers' closes a cycle: tiers → webhooks → tiers.\n"
            "  Drop one of the requires in that cycle.",
        ),
        (
            FEATURES + '[presets]\nminimal = ["accounts", "acount"]\n',
            "20: presets.minimal[1]: 'acount' isn't a feature of this catalog.\n  Did you mean 'accounts'?",
        ),
        (
            FEATURES + '[verify.sets]\nqueues = ["taskq"]\n',
            "20: verify.sets.queues[0]: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            FEATURES + '[[features.accounts.when]]\nif = "taskq"\n',
            "20: features.accounts.when[0].if: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            FEATURES + '[[features.accounts.when]]\nif = "taskiq.brokr=redis"\n',
            "20: features.accounts.when[0].if: 'taskiq.brokr' isn't an option of taskiq.\n  Did you mean 'broker'?",
        ),
        (
            FEATURES + '[[features.accounts.when]]\nif = "accounts.mode=fast"\n',
            "20: features.accounts.when[0].if: 'accounts.mode' isn't an option of accounts.\n  accounts has no options.",
        ),
        (
            FEATURES + '[[features.accounts.when]]\nif = "taskiq.broker=kafka"\n',
            "20: features.accounts.when[0].if: 'kafka' isn't a choice of taskiq.broker.\n  The choices are redis, rabbitmq.",
        ),
        (
            FEATURES + '[[features.accounts.when]]\nif = "nope.broker=redis"\n',
            "20: features.accounts.when[0].if: 'nope' isn't a feature of this catalog.\n  The features are accounts, taskiq.",
        ),
        (
            FEATURES + '[[features.accounts.when]]\nif = "nope or nope.broker=redis"\n',
            "20: features.accounts.when[0].if: 'nope' isn't a feature of this catalog.\n  The features are accounts, taskiq.",
        ),
        (
            FEATURES + '[[features.accounts.when]]\nif = "taskq and not taskq"\n',
            "20: features.accounts.when[0].if: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            FEATURES + '[features.accounts.contributes]\nrouters = [{ ref = "m:r", if = "taskq" }]\n',
            "20: features.accounts.contributes.routers[0].if: 'taskq' isn't a feature of this catalog.\n"
            "  Did you mean 'taskiq'?",
        ),
        (
            FEATURES
            + '[[features.accounts.when]]\nif = "taskiq"\ncontributes = { routers = [{ ref = "m:r", if = "taskq" }] }\n',
            "21: features.accounts.when[0].contributes.routers[0].if: 'taskq' isn't a feature of this catalog.\n"
            "  Did you mean 'taskiq'?",
        ),
        (
            FEATURES + '[core.contributes]\nrouters = [{ ref = "m:r", if = "taskq" }]\n',
            "20: core.contributes.routers[0].if: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            FEATURES
            + '[[wiring]]\npath = "backend/src/w.py"\ndoc = "W."\nif = "taskq"\n'
            + '[[wiring.slots]]\nkind = "raw"\ncode = "X = 1"\n',
            "22: wiring[0].if: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            FEATURES + '[[verify.checks]]\nname = "q"\nrun = ["true"]\nif = "taskq"\n',
            "22: verify.checks[0].if: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            FEATURES
            + '[generators.module]\ntemplates = "t"\n'
            + '[generators.module.register]\ncontributes = { routers = [{ ref = "src.{name}:r", if = "nope" }] }\n',
            "22: generators.module.register.contributes.routers[0].if: 'nope' isn't a feature of this catalog.\n"
            "  The features are accounts, taskiq.",
        ),
        (
            FEATURES + '[plugin_api]\nversion = 1\npublic_with = { taskq = ["src.tasks"] }\n',
            "21: plugin_api.public_with.taskq: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
    ],
)
def test_a_name_that_refers_to_nothing_is_refused_at_its_line(body: str, problem: str) -> None:
    assert _problems(MINIMAL + body + ROUTERS_SLOT) == [f"blueprint.toml:{problem}"]


def test_a_catalog_without_features_says_so_in_the_fix() -> None:
    body = '[presets]\nminimal = ["accounts"]\n'

    assert _problems(MINIMAL + body) == [
        "blueprint.toml:7: presets.minimal[0]: 'accounts' isn't a feature of this catalog.\n"
        "  This catalog declares no features yet.",
    ]


def test_every_unknown_name_in_one_condition_is_reported() -> None:
    body = FEATURES + '[[features.accounts.when]]\nif = "taskq or acounts"\n' + ROUTERS_SLOT

    assert _problems(MINIMAL + body) == [
        "blueprint.toml:20: features.accounts.when[0].if: 'acounts' isn't a feature of this catalog.\n"
        "  Did you mean 'accounts'?",
        "blueprint.toml:20: features.accounts.when[0].if: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
    ]


def test_names_are_checked_only_once_every_key_reads_cleanly() -> None:
    body = FEATURES.replace('requires = ["accounts"]', 'requires = ["acounts"]') + "[presets]\nminimal = 1\n"

    assert _problems(MINIMAL + body) == ["blueprint.toml:20: presets.minimal: should be a list of strings, not an integer."]


def test_an_extending_catalog_loads_with_names_its_base_may_hold() -> None:
    text = MINIMAL + EXTENDS + FEATURES.replace('requires = ["accounts"]', 'requires = ["jwt"]')

    assert _load(text).features[1].requires == ("jwt",)


def test_an_extending_catalog_says_its_names_wait_for_the_merge() -> None:
    assert blueprint.not_checked("fastro") == (
        "Not checked yet: names that refer to another section, since this catalog extends fastro, and those are "
        "checked on the merged catalog (extends, not built yet) and whether a declared command shadows a built-in "
        "fastro command."
    )


WIRED = """\
[points.routers]
cardinality = "many"

[points.cache]
cardinality = "one"

[points.labels]
cardinality = "many"

[points.menu]
cardinality = "many"

[features.accounts]
summary = "Users and login"

[features.taskiq]
summary = "Background tasks"

[[wiring]]
path = "backend/src/w.py"
doc = "W."

[[wiring.slots]]
kind = "tuple"
name = "ROUTERS"
point = "routers"
annotation = "tuple[object, ...]"
item = 'Mount({name}, "{prefix}", {weight})'
defaults = { weight = 1 }

[[wiring.slots]]
kind = "value"
name = "CACHE"
point = "cache"
annotation = "object"
fallback = "None"

[[wiring.slots]]
kind = "list"
name = "LABELS"
point = "labels"
annotation = "list[str]"
literal = true

[[wiring]]
path = "frontend/menu.ts"
doc = "Menu."

[[wiring.slots]]
kind = "data"
name = "MENU"
point = "menu"
format = "ts"
"""


def test_contributions_that_fit_their_slots_load() -> None:
    text = (
        MINIMAL
        + WIRED
        + "[features.accounts.contributes]\n"
        + 'routers = [{ ref = "m:r", prefix = "/a" }, { ref = "m:s", prefix = "", weight = 2 }]\n'
        + 'cache = ["m:redis"]\nlabels = ["Accounts", { value = "Users" }]\nmenu = [{ data = { href = "/a" } }]\n'
        + '[[features.taskiq.when]]\nif = "accounts"\ncontributes = { routers = [{ ref = "m:t", prefix = "/t" }] }\n'
        + '[generators.module]\ntemplates = "t"\n'
        + '[generators.module.register]\ncontributes = { routers = [{ ref = "src.{name}:r", prefix = "/{name_dashed}" }], '
        + 'cache = ["src.{name}:cache"] }\n'
        + '[plugin_api]\nversion = 1\npoints = ["routers", "labels"]\n'
    )

    assert set(_load(text).points) == {"routers", "cache", "labels", "menu"}


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (
            WIRED + '[features.accounts.contributes]\nrouter = [{ ref = "m:r", prefix = "" }]\n',
            "60: features.accounts.contributes.router: 'router' isn't a declared point.\n  Did you mean 'routers'?",
        ),
        (
            WIRED + '[[features.accounts.when]]\nif = "taskiq"\ncontributes = { queue = ["m:q"] }\n',
            "61: features.accounts.when[0].contributes.queue: 'queue' isn't a declared point.\n"
            "  The points are routers, cache, labels, menu.",
        ),
        (
            WIRED + '[core.contributes]\ncaches = ["m:c"]\n',
            "60: core.contributes.caches: 'caches' isn't a declared point.\n  Did you mean 'cache'?",
        ),
        (
            WIRED + '[generators.module]\ntemplates = "t"\n[generators.module.register]\ncontributes = { route = ["m:r"] }\n',
            "62: generators.module.register.contributes.route: 'route' isn't a declared point.\n  Did you mean 'routers'?",
        ),
        (
            WIRED.replace('point = "cache"', 'point = "caches"'),
            "9: points.cache: isn't collected by any slot.\n"
            "  Add a slot with point = 'cache' to a [[wiring]] file, or drop the point.\n"
            "blueprint.toml:39: wiring[0].slots[1].point: 'caches' isn't a declared point.\n  Did you mean 'cache'?",
        ),
        (
            WIRED + '[points.queue]\ncardinality = "many"\n',
            "59: points.queue: isn't collected by any slot.\n"
            "  Add a slot with point = 'queue' to a [[wiring]] file, or drop the point.",
        ),
        (
            WIRED.replace('point = "labels"', 'point = "routers"').replace("literal = true", 'item = "{name}"'),
            "12: points.labels: isn't collected by any slot.\n"
            "  Add a slot with point = 'labels' to a [[wiring]] file, or drop the point.\n"
            "blueprint.toml:46: wiring[0].slots[2].point: 'routers' is collected by wiring[0].slots[0] already.",
        ),
        (
            WIRED.replace('[points.cache]\ncardinality = "one"', '[points.cache]\ncardinality = "many"'),
            "39: wiring[0].slots[1].point: 'cache' is a many point, and a value slot collects a one point.\n"
            '  Set cardinality = "one" in [points.cache], or collect it with a tuple or list slot.',
        ),
        (
            WIRED
            + '[[wiring]]\npath = "backend/src/x.py"\ndoc = "X."\n'
            + '[[wiring.slots]]\nkind = "reexport"\nname = "Q"\npoint = "queue"\nfallback = "m:q"\n'
            + '[points.queue]\ncardinality = "many"\n',
            "65: wiring[2].slots[0].point: 'queue' is a many point, and a reexport slot collects a one point.\n"
            '  Set cardinality = "one" in [points.queue], or collect it with a tuple or list slot.',
        ),
        (
            WIRED + '[features.accounts.contributes]\ncache = ["m:a"]\n[features.taskiq.contributes]\ncache = ["m:b"]\n',
            "62: features.taskiq.contributes.cache: 'cache' is a one point, and feature accounts contributes to it already.",
        ),
        (
            WIRED
            + '[core.contributes]\ncache = ["m:a"]\n'
            + '[[features.taskiq.when]]\nif = "accounts"\ncontributes = { cache = ["m:b"] }\n',
            "63: features.taskiq.when[0].contributes.cache: 'cache' is a one point, and [core] contributes to it already.",
        ),
        (
            WIRED + '[features.accounts.contributes]\nrouters = ["m:r prefix"]\n',
            "60: features.accounts.contributes.routers[0]: 'm:r prefix' isn't a ref: "
            "expected 'package.module:name' or 'package.module:name as alias'.",
        ),
        (
            WIRED + '[features.accounts.contributes]\nrouters = [{ value = "x", prefix = "" }]\n',
            "60: features.accounts.contributes.routers[0]: should be a ref, since routers is collected by a tuple slot.",
        ),
        (
            WIRED + '[features.accounts.contributes]\nlabels = [{ ref = "m:l" }]\n',
            "60: features.accounts.contributes.labels[0]: should be a string or a value table, "
            "since labels is collected by a literal slot.",
        ),
        (
            WIRED + '[features.accounts.contributes]\nmenu = ["m:menu"]\n',
            "60: features.accounts.contributes.menu[0]: should be a data table, since menu is collected by a data slot.",
        ),
        (
            WIRED + '[features.accounts.contributes]\nrouters = ["m:r"]\n',
            "60: features.accounts.contributes.routers[0]: doesn't give prefix, which the item of wiring[0].slots[0] uses.\n"
            "  Add prefix to this contribution, or a default for it in that slot's defaults.",
        ),
        (
            WIRED + '[generators.module]\ntemplates = "t"\n[generators.module.register]\ncontributes = { routers = ["m:r"] }\n',
            "62: generators.module.register.contributes.routers[0]: doesn't give prefix, which the item of "
            "wiring[0].slots[0] uses.\n  Add prefix to this contribution, or a default for it in that slot's defaults.",
        ),
        (
            WIRED + '[plugin_api]\nversion = 1\npoints = ["routrs"]\n',
            "61: plugin_api.points[0]: 'routrs' isn't a declared point.\n  Did you mean 'routers'?",
        ),
        (
            WIRED + '[plugin_api]\nversion = 1\npoints = ["cache"]\n',
            "61: plugin_api.points[0]: 'cache' is a one point, and plugins contribute only to many points.\n"
            '  Set cardinality = "many" in [points.cache], or leave it out of plugin_api.points.',
        ),
    ],
)
def test_a_contribution_slot_or_point_that_doesnt_fit_is_refused_at_its_line(body: str, problem: str) -> None:
    assert "\n".join(_problems(MINIMAL + body)) == f"blueprint.toml:{problem}"


def test_an_item_may_convert_and_format_its_fields() -> None:
    text = MINIMAL + WIRED.replace("item = 'Mount({name}, \"{prefix}\", {weight})'", 'item = "Mount({name!r}, {weight:>3})"')

    assert _load(text).wiring[0].slots[0].item == "Mount({name!r}, {weight:>3})"


def test_a_catalog_without_points_says_so_in_the_fix() -> None:
    body = '[features.accounts]\nsummary = "Users"\n[features.accounts.contributes]\nrouters = ["m:r"]\n'

    assert _problems(MINIMAL + body) == [
        "blueprint.toml:9: features.accounts.contributes.routers: 'routers' isn't a declared point.\n"
        "  This catalog declares no points yet.",
    ]
