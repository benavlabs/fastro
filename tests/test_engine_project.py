"""Loading a project's ``fastro.toml`` (spec 3), key by key."""

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine.blueprint import Contribution
from fastro.engine.problems import CatalogError
from fastro.engine.project import App, Pin, Plugin, Project, load
from fastro.engine.spec import RepoPath

FULL = """\
blueprint = { name = "fastro", version = "0.21.0" }
source = true
features = ["accounts", "taskiq"]

[project]
name = "FastAPI_Boilerplate"

[options]
"taskiq.broker" = "redis"

[app]
requires = ["accounts"]
paths = ["backend/src/app/"]
contributes = { routers = ["src.app.routes:router"] }

[plugins]
stripe = "gh:benavlabs/fastro-stripe@v1.2.0"
audit = { source = "../audit#plugin", version = ">=1,<2" }

[checks]
skip = ["FR013"]
strict = true
"""

BOILERPLATE = """\
source = true
features = ["accounts", "rbac", "ratelimit", "tiers", "tier_limits", "api_keys", "admin", "cache", "taskiq"]
blueprint = { name = "fastro", version = "0.20.0" }

[project]
name = "fastapi-boilerplate"

[options]
"cache.backend" = "redis"
"taskiq.broker" = "redis"
"""


def _text(top: str = "", tables: str = "") -> str:
    return f'blueprint = {{ name = "fastro", version = "0.21.0" }}\nfeatures = []\n{top}[project]\nname = "api"\n{tables}'


def _problems(text: str) -> list[str]:
    with pytest.raises(CatalogError) as caught:
        load("fastro.toml", text)
    return [str(problem) for problem in caught.value.problems]


def test_every_key_is_read() -> None:
    project = load("fastro.toml", FULL)

    assert project == Project(
        blueprint=Pin("fastro", Version("0.21.0")),
        name="FastAPI_Boilerplate",
        features=("accounts", "taskiq"),
        source=True,
        options={("taskiq", "broker"): "redis"},
        app=App(
            requires=("accounts",),
            paths=(RepoPath("backend/src/app/"),),
            contributes={"routers": (Contribution("src.app.routes:router", "", at=("app", "contributes", "routers", 0)),)},
        ),
        plugins={
            "stripe": Plugin("stripe", "gh:benavlabs/fastro-stripe@v1.2.0", None, at=("plugins", "stripe")),
            "audit": Plugin("audit", "../audit#plugin", SpecifierSet(">=1,<2"), at=("plugins", "audit")),
        },
        skip=("FR013",),
        strict=True,
        document=project.document,
    )


def test_the_slug_is_the_name_normalized_as_pep_503_does() -> None:
    assert load("fastro.toml", FULL).slug == "fastapi-boilerplate"
    assert load("fastro.toml", _text().replace('"api"', '"My..API__v2"')).slug == "my-api-v2"


def test_a_project_with_only_the_required_keys_gets_the_defaults() -> None:
    project = load("fastro.toml", _text())

    assert (project.features, project.source, project.options, project.plugins, project.skip, project.strict) == (
        (),
        False,
        {},
        {},
        (),
        False,
    )
    assert project.app == App(requires=(), paths=(), contributes={})


def test_the_boilerplates_own_fastro_toml_loads() -> None:
    project = load("fastro.toml", BOILERPLATE)

    assert (project.blueprint, project.source, project.slug) == (Pin("fastro", Version("0.20.0")), True, "fastapi-boilerplate")
    assert project.options == {("cache", "backend"): "redis", ("taskiq", "broker"): "redis"}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            'features = []\n[project]\nname = "api"\n',
            '1: blueprint: is required.\n  Add blueprint = { name = "fastro", version = "0.21.0" }, '
            "naming the catalog this project was made from.",
        ),
        (
            'blueprint = { name = "fastro", version = "0.21.0" }\n[project]\nname = "api"\n',
            "1: features: is required.\n  Add features = [] for core only, or list the features this project uses.",
        ),
        (
            'blueprint = { name = "fastro", version = "0.21.0" }\nfeatures = []\n',
            '1: project: is required.\n  Add a [project] table with name, such as name = "my-api".',
        ),
        (
            'blueprint = { name = "fastro", version = "0.21.0" }\nfeatures = []\n[project]\n',
            "3: project.name: is required.\n  Add name to [project].",
        ),
        (
            _text().replace('"api"', '"-api"'),
            "4: project.name: '-api' isn't a project name: use letters, digits, '.', '_' and '-', "
            "starting and ending with a letter or digit, as PEP 508 says.",
        ),
        (
            _text().replace('"api"', '"my api"'),
            "4: project.name: 'my api' isn't a project name: use letters, digits, '.', '_' and '-', "
            "starting and ending with a letter or digit, as PEP 508 says.",
        ),
        (_text("sorce = true\n"), "3: sorce: isn't a top-level key of fastro.toml.\n  Did you mean 'source'?"),
        (
            _text("strictness = 1\n"),
            "3: strictness: isn't a top-level key of fastro.toml.\n"
            "  The top-level keys are app, blueprint, checks, features, options, plugins, project, source.",
        ),
        (_text('source = "yes"\n'), "3: source: should be true or false, not a string."),
        (
            _text().replace('version = "0.21.0"', 'version = ">=0.21"'),
            "1: blueprint.version: '>=0.21' isn't a PEP 440 version, such as '0.21.0'.",
        ),
        (
            _text().replace('name = "fastro"', 'name = "Fastro"'),
            "1: blueprint.name: 'Fastro' isn't a valid blueprint name: use lowercase letters, digits and hyphens, "
            "starting with a letter.",
        ),
        (
            _text().replace(', version = "0.21.0"', ""),
            "1: blueprint.version: is required.\n  Add version to [blueprint].",
        ),
        (
            _text().replace('{ name = "fastro", version = "0.21.0" }', '"fastro"'),
            "1: blueprint: should be a table, not a string.",
        ),
        (
            _text().replace("features = []", 'features = ["accounts", "tiers", "accounts"]'),
            "2: features[2]: 'accounts' is listed twice.",
        ),
        (
            _text().replace("features = []", 'features = ["Accounts"]'),
            "2: features[0]: 'Accounts' isn't a valid name: use lowercase letters, digits and underscores, "
            "starting with a letter.",
        ),
        (
            _text(tables='[options]\ncache.backend = "redis"\n'),
            '6: options.cache: should be a string, not a table.\n  Quote the whole key, such as "cache.backend" = "redis".',
        ),
        (
            _text(tables='[options]\n"cache" = "redis"\n'),
            "6: options.cache: 'cache' isn't a feature.option key, such as 'cache.backend'.",
        ),
        (
            _text(tables='[options]\n"cache.Backend" = "redis"\n'),
            "6: options.\"cache.Backend\": 'Backend' isn't a valid name: use lowercase letters, digits and underscores, "
            "starting with a letter.",
        ),
        (_text(tables='[options]\n"cache.backend" = 1\n'), '6: options."cache.backend": should be a string, not an integer.'),
        (
            _text(tables='[app]\nrequire = ["accounts"]\n'),
            "6: app.require: isn't a key of [app].\n  Did you mean 'requires'?",
        ),
        (
            _text(tables='[app]\npaths = ["../x"]\n'),
            "6: app.paths[0]: '../x' isn't a repo path: it's climbing out with '..'. Write it relative to the repo root, "
            "with '/'.",
        ),
        (
            _text(tables="[app]\ncontributes = { routers = [{ order = 1 }] }\n"),
            "6: app.contributes.routers[0]: needs one of ref, value and data.",
        ),
        (
            _text(tables='[plugins]\nStripe = "stripe"\n'),
            "6: plugins.Stripe: 'Stripe' isn't a valid name: use lowercase letters, digits and underscores, "
            "starting with a letter.",
        ),
        (
            _text(tables='[plugins]\nstripe = "https://example.com/stripe"\n'),
            "6: plugins.stripe: 'https://example.com/stripe' isn't a source: write a path (absolute, ./ or ../), "
            "gh:OWNER/REPO[@REF], git+ssh://… or git+https://…, or a registry name, then an optional #SUBDIR.",
        ),
        (
            _text(tables='[plugins]\nstripe = { version = ">=1" }\n'),
            "6: plugins.stripe.source: is required.\n  Add source to [plugins.stripe].",
        ),
        (
            _text(tables='[plugins]\nstripe = { source = "stripe", version = "1" }\n'),
            "6: plugins.stripe.version: '1' isn't a PEP 440 version specifier, such as '>=0.20,<0.22'.",
        ),
        (
            _text(tables="[plugins]\nstripe = 1\n"),
            "6: plugins.stripe: should be a source string or a table, not an integer.",
        ),
        (
            _text(tables='[checks]\nskip = ["fr013"]\n'),
            "6: checks.skip[0]: 'fr013' isn't a check code, such as 'FR013'.",
        ),
        (_text(tables='[checks]\nstrict = "yes"\n'), "6: checks.strict: should be true or false, not a string."),
        (
            _text(tables="[checks]\nfail_fast = true\n"),
            "6: checks.fail_fast: isn't a key of [checks].\n  The keys of [checks] are skip, strict.",
        ),
    ],
)
def test_a_key_that_breaks_its_rule_is_refused_at_its_line(text: str, problem: str) -> None:
    assert _problems(text) == [f"fastro.toml:{problem}"]
