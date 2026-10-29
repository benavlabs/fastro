"""Loading a plugin's ``fastro-plugin.toml`` (spec 5), key by key."""

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine.plugins import PluginInfo, Target, load
from fastro.engine.problems import CatalogError
from fastro.engine.spec import RepoPath

FULL = """\
[plugin]
name = "stripe"
version = "1.2.0"
summary = "Stripe billing"
license = "MIT"
homepage = "https://example.com/stripe"
group = "Billing"

[plugin.targets.fastro]
blueprint = ">=0.21,<0.23"
api = 1

[plugin.targets.fastroai]
blueprint = ">=0.3"
api = 2

[feature]
summary = "Payments with Stripe"
requires = ["accounts"]
paths = ["backend/src/plugins/stripe/"]
env = "env.fragment"
contributes = { routers = ["src.plugins.stripe.routes:router"] }

[commands."stripe sync"]
module = "src.plugins.stripe.sync"

[generators.stripe-webhook]
templates = "templates/webhook/"
"""


def _text(plugin: str = "", feature: str = "", tables: str = "") -> str:
    return (
        f'[plugin]\nname = "stripe"\nversion = "1.2.0"\nsummary = "Stripe billing"\n{plugin}'
        '[plugin.targets.fastro]\nblueprint = ">=0.21"\napi = 1\n'
        f'[feature]\nsummary = "Payments"\n{feature}{tables}'
    )


def _problems(text: str) -> list[str]:
    with pytest.raises(CatalogError) as caught:
        load("fastro-plugin.toml", text)
    return [str(problem) for problem in caught.value.problems]


def test_every_key_is_read() -> None:
    manifest = load("fastro-plugin.toml", FULL)

    assert manifest.plugin == PluginInfo(
        name="stripe",
        version=Version("1.2.0"),
        summary="Stripe billing",
        license="MIT",
        homepage="https://example.com/stripe",
        group="Billing",
        targets={"fastro": Target(SpecifierSet(">=0.21,<0.23"), 1), "fastroai": Target(SpecifierSet(">=0.3"), 2)},
    )
    assert (manifest.feature.name, manifest.feature.summary, manifest.feature.requires) == (
        "stripe",
        "Payments with Stripe",
        ("accounts",),
    )
    assert manifest.feature.paths == (RepoPath("backend/src/plugins/stripe/"),)
    assert list(manifest.feature.contributes) == ["routers"]
    assert manifest.env == RepoPath("env.fragment")
    assert manifest.commands[("stripe", "sync")].module == "src.plugins.stripe.sync"
    assert manifest.generators["stripe-webhook"].templates == RepoPath("templates/webhook/")


def test_a_manifest_with_only_the_required_keys_gets_the_defaults() -> None:
    manifest = load("fastro-plugin.toml", _text())

    assert (manifest.plugin.license, manifest.plugin.homepage, manifest.plugin.group) == ("", "", "")
    assert (manifest.env, manifest.commands, manifest.generators) == (None, {}, {})


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            '[feature]\nsummary = "Payments"\n',
            "1: plugin: is required.\n  Add a [plugin] table with name, version, summary and a target.",
        ),
        (
            _text().split("[feature]")[0],
            "1: feature: is required.\n  Add a [feature] table with summary, as for a blueprint's [features.<name>].",
        ),
        ('plugin = 1\n[feature]\nsummary = "Payments"\n', "1: plugin: should be a table, not an integer."),
        (
            'feature = "payments"\n' + _text().split("[feature]")[0],
            "1: feature: should be a table, not a string.",
        ),
        (
            _text("targets = { fastro = 1 }\n").replace('[plugin.targets.fastro]\nblueprint = ">=0.21"\napi = 1\n', ""),
            "5: plugin.targets.fastro: should be a table, not an integer.",
        ),
        (_text().replace('"stripe"', '"core"'), "2: plugin.name: 'core' is reserved for the files no feature owns."),
        (
            _text().replace('"stripe"', '"Stripe"'),
            "2: plugin.name: 'Stripe' isn't a valid name: use lowercase letters, digits and underscores, "
            "starting with a letter.",
        ),
        (
            _text().replace('version = "1.2.0"', 'version = ">=1"'),
            "3: plugin.version: '>=1' isn't a PEP 440 version, such as '0.21.0'.",
        ),
        (_text().replace('summary = "Stripe billing"\n', ""), "1: plugin.summary: is required.\n  Add summary to [plugin]."),
        (_text("license = 1\n"), "5: plugin.license: should be a string, not an integer."),
        (
            _text('author = "me"\n'),
            "5: plugin.author: isn't a key of [plugin].\n"
            "  The keys of [plugin] are group, homepage, license, name, summary, targets, version.",
        ),
        (
            _text().replace('[plugin.targets.fastro]\nblueprint = ">=0.21"\napi = 1\n', ""),
            "1: plugin.targets: is required.\n  Add a [plugin.targets.fastro] table with blueprint and api.",
        ),
        (
            _text("targets = {}\n").replace('[plugin.targets.fastro]\nblueprint = ">=0.21"\napi = 1\n', ""),
            "5: plugin.targets: should list at least one target.\n"
            "  Add a [plugin.targets.fastro] table with blueprint and api.",
        ),
        (
            _text().replace("[plugin.targets.fastro]", "[plugin.targets.Fastro]"),
            "5: plugin.targets.Fastro: 'Fastro' isn't a valid blueprint name: use lowercase letters, digits and hyphens, "
            "starting with a letter.",
        ),
        (
            _text().replace('blueprint = ">=0.21"', 'blueprint = "0.21"'),
            "6: plugin.targets.fastro.blueprint: '0.21' isn't a PEP 440 version specifier, such as '>=0.20,<0.22'.",
        ),
        (_text().replace("api = 1", "api = 0"), "7: plugin.targets.fastro.api: should be 1 or more, not 0."),
        (
            _text().replace("api = 1\n", ""),
            "5: plugin.targets.fastro.api: is required.\n  Add api to [plugin.targets.fastro].",
        ),
        (
            _text(feature='[feature.commands."stripe sync"]\nmodule = "m"\n'),
            "10: feature.commands: isn't allowed in a manifest: a plugin's commands go in its top-level [commands] table.",
        ),
        (
            _text().replace('summary = "Payments"\n', ""),
            "8: feature.summary: is required.\n  Add summary to [feature].",
        ),
        (
            _text(feature='env = "env/"\n'),
            "10: feature.env: 'env/' names a directory, and env names a file.",
        ),
        (
            _text(feature='env = "../env"\n'),
            "10: feature.env: '../env' isn't a repo path: it's climbing out with '..'. Write it relative to the directory "
            "holding fastro-plugin.toml, with '/'.",
        ),
        (
            _text(feature='name = "stripe"\n'),
            "10: feature.name: isn't a key of [feature].\n  The keys of [feature] are contributes, dependencies, "
            "dev_dependencies, env, group, options, paths, requires, services, summary, when.",
        ),
        (
            _text(tables="[extra]\n"),
            "10: extra: isn't a top-level key of fastro-plugin.toml.\n"
            "  The top-level keys are commands, feature, generators, plugin.",
        ),
        (
            _text(tables='[commands."Stripe"]\nmodule = "m"\n'),
            "10: commands.Stripe: 'Stripe' isn't a command: write its words in lowercase letters, digits and hyphens, "
            "one space apart, such as 'env validate'.",
        ),
        (
            _text(tables='[generators.Webhook]\ntemplates = "t"\n'),
            "10: generators.Webhook: 'Webhook' isn't a valid generator name: use lowercase letters, digits and hyphens, "
            "starting with a letter.",
        ),
    ],
)
def test_a_key_that_breaks_its_rule_is_refused_at_its_line(text: str, problem: str) -> None:
    assert _problems(text) == [f"fastro-plugin.toml:{problem}"]
