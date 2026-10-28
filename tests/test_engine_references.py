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

    assert [feature.name for feature in _load(text).features] == ["accounts", "taskiq"]


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
    assert _problems(MINIMAL + body) == [f"blueprint.toml:{problem}"]


def test_a_catalog_without_features_says_so_in_the_fix() -> None:
    body = '[presets]\nminimal = ["accounts"]\n'

    assert _problems(MINIMAL + body) == [
        "blueprint.toml:7: presets.minimal[0]: 'accounts' isn't a feature of this catalog.\n"
        "  This catalog declares no features yet.",
    ]


def test_every_unknown_name_in_one_condition_is_reported() -> None:
    body = FEATURES + '[[features.accounts.when]]\nif = "taskq or acounts"\n'

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
