"""Loading a project's ``fastro.toml`` (spec 3), key by key."""

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint, Contribution
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


CATALOG = """\
[blueprint]
name = "mini"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[points.routers]
cardinality = "many"

[points.cache]
cardinality = "one"

[features.accounts]
summary = "Users and login"
paths = ["backend/src/modules/user/"]

[features.taskiq]
summary = "Background tasks"

[features.taskiq.options.broker]
choices = ["redis", "rabbitmq"]
default = "redis"

[[wiring]]
path = "backend/src/wiring.py"
doc = "Wiring."

[[wiring.slots]]
kind = "tuple"
name = "ROUTERS"
point = "routers"
annotation = "tuple[object, ...]"

[[wiring.slots]]
kind = "value"
name = "CACHE"
point = "cache"
annotation = "object"
fallback = "None"
"""


def _catalog(text: str = CATALOG) -> Blueprint:
    return blueprint.load("blueprint.toml", text, Version("0.1.0"))


def _for_mini(top: str = "", tables: str = "") -> str:
    return _text(top, tables).replace('name = "fastro", version = "0.21.0"', 'name = "mini", version = "0.1.0"')


def _refused(text: str, catalog: Blueprint | None = None) -> CatalogError:
    with pytest.raises(CatalogError) as caught:
        load("fastro.toml", text, catalog or _catalog())
    return caught.value


def _checked(text: str, catalog: Blueprint | None = None) -> list[str]:
    return [str(problem) for problem in _refused(text, catalog).problems]


def test_a_project_whose_names_its_catalog_declares_loads() -> None:
    text = _for_mini().replace("features = []", 'features = ["accounts", "taskiq"]') + (
        '[options]\n"taskiq.broker" = "rabbitmq"\n'
        '[app]\nrequires = ["accounts"]\npaths = ["backend/src/app/", "backend/src/modules/user"]\n'
        'contributes = { routers = [{ ref = "src.app:router", if = "taskiq.broker=rabbitmq" }], cache = ["src.app:cache"] }\n'
    )

    assert load("fastro.toml", text, _catalog()).app.requires == ("accounts",)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            _for_mini().replace('name = "mini"', 'name = "fastro"'),
            "1: blueprint.name: is 'fastro', and the catalog is 'mini'.\n"
            "  Set blueprint.name in fastro.toml to 'mini', the catalog's name.",
        ),
        (
            _for_mini().replace('version = "0.1.0"', 'version = "0.1.1"'),
            "1: blueprint.version: is 0.1.1, and the catalog is 0.1.0.\n"
            "  Set blueprint.version in fastro.toml to 0.1.0, the catalog's version.",
        ),
        (
            _for_mini().replace("features = []", 'features = ["acounts"]'),
            "2: features[0]: 'acounts' isn't a feature of this catalog.\n  Did you mean 'accounts'?",
        ),
        (
            _for_mini(tables='[app]\nrequires = ["taskq"]\n'),
            "6: app.requires[0]: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            _for_mini(tables='[options]\n"taskq.broker" = "redis"\n'),
            "6: options.\"taskq.broker\": 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            _for_mini(tables='[options]\n"taskiq.brokr" = "redis"\n'),
            "6: options.\"taskiq.brokr\": 'taskiq.brokr' isn't an option of taskiq.\n  Did you mean 'broker'?",
        ),
        (
            _for_mini(tables='[options]\n"taskiq.broker" = "kafka"\n'),
            "6: options.\"taskiq.broker\": 'kafka' isn't a choice of taskiq.broker.\n  The choices are redis, rabbitmq.",
        ),
        (
            _for_mini(tables='[app]\ncontributes = { router = ["src.app:r"] }\n'),
            "6: app.contributes.router: 'router' isn't a declared point.\n  Did you mean 'routers'?",
        ),
        (
            _for_mini(tables='[app]\ncontributes = { routers = [{ value = "x" }] }\n'),
            "6: app.contributes.routers[0]: should be a ref, since routers is collected by a tuple slot.",
        ),
        (
            _for_mini(tables='[app]\ncontributes = { routers = [{ ref = "src.app:r", if = "taskq" }] }\n'),
            "6: app.contributes.routers[0].if: 'taskq' isn't a feature of this catalog.\n  Did you mean 'taskiq'?",
        ),
        (
            _for_mini(tables='[app]\npaths = ["backend/src/"]\n'),
            "6: app.paths[0]: 'backend/src/' covers 'backend/src/wiring.py', a wiring file the tool generates.\n"
            "  Narrow [app].paths so they leave the wiring files out.",
        ),
        (
            _for_mini(tables='[app]\npaths = ["backend/src/wiring.py"]\n'),
            "6: app.paths[0]: 'backend/src/wiring.py' is a wiring file the tool generates.\n"
            "  Narrow [app].paths so they leave the wiring files out.",
        ),
        (
            _for_mini(tables='[app]\npaths = ["backend/src/app/", "backend/src/app/"]\n'),
            "6: app.paths[1]: 'backend/src/app/' is declared by app.paths[0] already.\n"
            "  A path belongs to one declaration: drop one of them.",
        ),
        (
            _for_mini(tables='[app]\npaths = ["backend/src/modules/user/"]\n'),
            "6: app.paths[0]: 'backend/src/modules/user/' is declared by blueprint.toml's features.accounts.paths[0] "
            "already.\n  A path belongs to one declaration: drop one of them.",
        ),
    ],
)
def test_a_name_its_catalog_doesnt_declare_is_refused_at_its_line(text: str, problem: str) -> None:
    assert _checked(text) == [f"fastro.toml:{problem}"]


def test_a_catalog_key_in_a_message_about_fastro_toml_names_the_catalog_file() -> None:
    catalog = _catalog(
        CATALOG.replace(
            'annotation = "tuple[object, ...]"', 'annotation = "tuple[object, ...]"\nitem = "Mount({name}, {prefix})"'
        )
    )
    text = _for_mini(tables='[app]\ncontributes = { routers = ["src.app:r"] }\n')

    assert _checked(text, catalog) == [
        "fastro.toml:6: app.contributes.routers[0]: doesn't give prefix, which the item of blueprint.toml's "
        "wiring[0].slots[0] uses.\n  Add prefix to this contribution, or a default for it in that slot's defaults."
    ]


def test_names_are_checked_only_once_every_key_reads_cleanly() -> None:
    text = _for_mini().replace("features = []", 'features = ["acounts"]').replace('"api"', '"-api"')

    assert [problem.key for problem in _refused(text).problems] == ["project.name"]


def test_against_a_catalog_that_extends_another_only_the_pin_is_checked() -> None:
    extending = _catalog(CATALOG.replace("schema = 1\n", 'schema = 1\nextends = { name = "fastro", version = "0.21.0" }\n'))
    text = _for_mini().replace("features = []", 'features = ["jwt"]')

    assert load("fastro.toml", text, extending).features == ("jwt",)
    refused = _refused(text.replace('version = "0.1.0"', 'version = "9.0"'), extending)

    assert [problem.key for problem in refused.problems] == ["blueprint.version"]
