"""Loading ``blueprint.toml``: spec sections 2.1 to 2.5, with unknown keys refused (1.8)."""

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine.blueprint import (
    Blueprint,
    Check,
    Contribution,
    Extends,
    Feature,
    Option,
    Point,
    When,
    load,
)
from fastro.engine.conditions import parse
from fastro.engine.problems import CatalogError
from fastro.engine.spec import Ref, RepoPath

MINIMAL = """\
[blueprint]
name = "mini"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"
"""

FULL = """\
[blueprint]
name = "mini-ai"
version = "0.2.0rc1"
schema = 1
requires_fastro = ">=0.1.0.dev0,<0.2"
python_root = "server"
import_root = "app_src"
blueprint_only = ["docs/", "blueprint.toml"]
overlay = "blueprint/overlay/"
extends = { name = "mini", version = "0.1.0" }

[blueprint.dependencies]
runtime = ["fastapi[standard]>=0.115.8"]
dev = ["pytest>=8"]

[presets]
minimal = ["accounts"]
full = ["accounts", "taskiq"]

[verify]
include_presets = false

[verify.sets]
core-only = []
accounts_taskiq = ["accounts", "taskiq"]

[[verify.checks]]
name = "frontend"
run = ["npm", "run", "build"]
cwd = "frontend/"
if = "accounts"

[[verify.checks]]
name = "lint"
run = ["ruff", "check"]

[points.routers]
cardinality = "many"
doc = "Routers mounted on the API."

[points.email_sender]
cardinality = "one"

[core.contributes]
readiness = ["src.core.health:database"]

[features.accounts]
summary = "Users and login"
group = "Identity"
requires = []
paths = ["server/src/modules/accounts/"]
dependencies = ["argon2-cffi>=23"]
dev_dependencies = ["freezegun"]
services = ["postgres"]

[features.accounts.contributes]
routers = [
  "src.modules.accounts.routes:router as accounts_router",
  { ref = "src.accounts.admin:router", order = 50, if = "taskiq", call = true, prefix = "/a", throttled = false, weight = 3 },
]
labels = [{ value = "Accounts" }]
menu = [{ data = { href = "/accounts", label = "Accounts" } }]

[features.taskiq]
summary = "Background tasks"
requires = ["accounts"]

[features.taskiq.options.broker]
choices = ["redis", "rabbitmq"]
default = "redis"
env = { TASKIQ_BROKER_TYPE = "{value}" }
services = { rabbitmq = ["rabbitmq"] }
dependencies = { rabbitmq = ["aio-pika>=9"] }

[[features.taskiq.when]]
if = "accounts and taskiq.broker=redis"
paths = ["server/src/infrastructure/taskiq/email.py"]
dependencies = ["redis>=5"]
dev_dependencies = ["fakeredis"]
services = ["redis"]

[features.taskiq.when.contributes]
email_sender = ["src.infrastructure.taskiq.email:queued_sender"]
"""


TOOL = Version("0.1.0")
UPGRADE = "  Upgrade fastro (uv tool upgrade fastro), then run this again."


def _load(text: str, tool: Version = TOOL) -> Blueprint:
    return load("blueprint.toml", text, tool)


def _problems(text: str, tool: Version = TOOL) -> list[str]:
    with pytest.raises(CatalogError) as caught:
        _load(text, tool)
    return [str(problem) for problem in caught.value.problems]


def test_the_blueprint_table_is_read_with_every_key() -> None:
    info = _load(FULL).info

    assert info.name == "mini-ai"
    assert info.version == Version("0.2.0rc1")
    assert info.schema == 1
    assert info.requires_fastro == SpecifierSet(">=0.1.0.dev0,<0.2")
    assert info.python_root == RepoPath("server")
    assert info.import_root == "app_src"
    assert info.blueprint_only == (RepoPath("docs/"), RepoPath("blueprint.toml"))
    assert info.overlay == RepoPath("blueprint/overlay/")
    assert info.extends == Extends("mini", Version("0.1.0"))
    assert info.dependencies == (Requirement("fastapi[standard]>=0.115.8"),)
    assert info.dev_dependencies == (Requirement("pytest>=8"),)


def test_the_blueprint_tables_optional_keys_have_the_specs_defaults() -> None:
    blueprint = _load(MINIMAL)
    info = blueprint.info

    assert (info.python_root, info.import_root) == (RepoPath("backend"), "src")
    assert (info.blueprint_only, info.overlay, info.extends) == ((), None, None)
    assert (info.dependencies, info.dev_dependencies) == ((), ())
    assert (blueprint.presets, blueprint.points, blueprint.features, blueprint.core) == ({}, {}, (), {})
    assert (blueprint.verify.include_presets, blueprint.verify.sets, blueprint.verify.checks) == (True, {}, ())


def test_presets_and_verify_are_read() -> None:
    blueprint = _load(FULL)

    assert blueprint.presets == {"minimal": ("accounts",), "full": ("accounts", "taskiq")}
    assert blueprint.verify.include_presets is False
    assert blueprint.verify.sets == {"core-only": (), "accounts_taskiq": ("accounts", "taskiq")}
    assert blueprint.verify.checks == (
        Check("frontend", ("npm", "run", "build"), RepoPath("frontend/"), parse("accounts"), ("verify", "checks", 0)),
        Check("lint", ("ruff", "check"), None, None, ("verify", "checks", 1)),
    )


def test_points_are_read() -> None:
    assert _load(FULL).points == {
        "routers": Point("routers", "many", "Routers mounted on the API.", ("points", "routers")),
        "email_sender": Point("email_sender", "one", "", ("points", "email_sender")),
    }


def test_core_contributions_are_read() -> None:
    assert _load(FULL).core == {
        "readiness": (Contribution("src.core.health:database", "", at=("core", "contributes", "readiness", 0)),),
    }


def test_features_are_read_in_declaration_order_with_every_key() -> None:
    accounts, taskiq = _load(FULL).features
    contributes = ("features", "accounts", "contributes")

    assert accounts == Feature(
        name="accounts",
        summary="Users and login",
        group="Identity",
        requires=(),
        paths=(RepoPath("server/src/modules/accounts/"),),
        dependencies=(Requirement("argon2-cffi>=23"),),
        dev_dependencies=(Requirement("freezegun"),),
        services=("postgres",),
        contributes={
            "routers": (
                Contribution("src.modules.accounts.routes:router as accounts_router", "", at=(*contributes, "routers", 0)),
                Contribution(
                    Ref.parse("src.accounts.admin:router"),
                    "ref",
                    order=50,
                    condition=parse("taskiq"),
                    call=True,
                    fields={"prefix": "/a", "throttled": False, "weight": 3},
                    at=(*contributes, "routers", 1),
                ),
            ),
            "labels": (Contribution("Accounts", "value", at=(*contributes, "labels", 0)),),
            "menu": (Contribution({"href": "/accounts", "label": "Accounts"}, "data", at=(*contributes, "menu", 0)),),
        },
        options={},
        when=(),
        at=("features", "accounts"),
    )
    assert (taskiq.name, taskiq.summary, taskiq.group, taskiq.requires) == ("taskiq", "Background tasks", "", ("accounts",))


def test_options_are_read() -> None:
    taskiq = _load(FULL).features[1]

    assert taskiq.options == {
        "broker": Option(
            name="broker",
            choices=("redis", "rabbitmq"),
            default="redis",
            env={"TASKIQ_BROKER_TYPE": "{value}"},
            services={"rabbitmq": ("rabbitmq",)},
            dependencies={"rabbitmq": (Requirement("aio-pika>=9"),)},
            at=("features", "taskiq", "options", "broker"),
        )
    }


def test_when_tables_are_read() -> None:
    taskiq = _load(FULL).features[1]
    at = ("features", "taskiq", "when", 0)

    assert taskiq.when == (
        When(
            condition=parse("accounts and taskiq.broker=redis"),
            paths=(RepoPath("server/src/infrastructure/taskiq/email.py"),),
            dependencies=(Requirement("redis>=5"),),
            dev_dependencies=(Requirement("fakeredis"),),
            services=("redis",),
            contributes={
                "email_sender": (
                    Contribution(
                        "src.infrastructure.taskiq.email:queued_sender", "", at=(*at, "contributes", "email_sender", 0)
                    ),
                )
            },
            at=at,
        ),
    )


def test_the_tables_of_later_sections_are_known_top_level_keys() -> None:
    text = MINIMAL + "[wiring_defaults]\n[checks]\n[plugin_api]\n[generators]\n[commands]\n[[wiring]]\n"

    assert _load(text).info.name == "mini"


def test_a_file_without_a_blueprint_table_is_refused() -> None:
    assert _problems("[presets]\n") == [
        "blueprint.toml:1: blueprint: is required.\n  Add a [blueprint] table with name, version, schema and requires_fastro."
    ]


def test_every_required_key_of_the_blueprint_table_is_reported_at_once() -> None:
    assert _problems('[blueprint]\nname = "mini"\n') == [
        "blueprint.toml:1: blueprint.requires_fastro: is required.\n  Add requires_fastro to [blueprint].",
        "blueprint.toml:1: blueprint.schema: is required.\n  Add schema to [blueprint].",
        "blueprint.toml:1: blueprint.version: is required.\n  Add version to [blueprint].",
    ]


def test_an_option_without_valid_choices_reports_only_its_choices() -> None:
    text = MINIMAL + FEATURE + '[features.a.options.broker]\nchoices = []\ndefault = "x"\nservices = { x = ["y"] }\n'

    assert _problems(text) == ["blueprint.toml:9: features.a.options.broker.choices: should list at least one choice."]


def test_a_blueprint_key_that_isnt_a_table_is_refused() -> None:
    assert _problems("blueprint = 1\n") == ["blueprint.toml:1: blueprint: should be a table, not an integer."]


def test_a_missing_name_is_reported_once() -> None:
    assert _problems(MINIMAL.replace('name = "mini"\n', "")) == [
        "blueprint.toml:1: blueprint.name: is required.\n  Add name to [blueprint]."
    ]


def test_a_value_of_the_wrong_kind_is_named_by_its_toml_kind() -> None:
    assert _problems(MINIMAL + '[features.a]\nsummary = "A"\ngroup = 1979-05-27\n') == [
        "blueprint.toml:8: features.a.group: should be a string, not a date or time."
    ]


UNKNOWN_KEYS = 'new_table = {}\n[features.a]\nsummary = "A"\nnew_key = 1\n'


def test_a_newer_schema_stops_the_load_with_only_the_upgrade() -> None:
    text = MINIMAL.replace("schema = 1", "schema = 2") + UNKNOWN_KEYS

    assert _problems(text) == [f"blueprint.toml:4: blueprint.schema: is 2, and this fastro reads schema 1.\n{UPGRADE}"]


def test_a_schema_below_1_is_refused_without_suggesting_an_upgrade() -> None:
    text = MINIMAL.replace("schema = 1", "schema = 0") + UNKNOWN_KEYS

    assert _problems(text) == ["blueprint.toml:4: blueprint.schema: should be 1, not 0.\n  Set schema = 1 in [blueprint]."]


@pytest.mark.parametrize("requires", [">=0.2", "==0.2.*", ">=0.1.1,<0.3", "~=0.2.0"])
def test_a_catalog_for_a_newer_fastro_stops_the_load_with_only_the_upgrade(requires: str) -> None:
    text = MINIMAL.replace(">=0.1.0.dev0", requires) + UNKNOWN_KEYS

    assert _problems(text) == [
        f"blueprint.toml:5: blueprint.requires_fastro: is {requires!r}, which doesn't include this fastro, 0.1.0.\n{UPGRADE}"
    ]


@pytest.mark.parametrize("requires", ["<0.1", "==0.0.5", "!=0.1.0", ">=0.0.1,<0.1"])
def test_a_catalog_for_an_older_fastro_names_the_one_to_install(requires: str) -> None:
    text = MINIMAL.replace(">=0.1.0.dev0", requires) + UNKNOWN_KEYS

    assert _problems(text) == [
        f"blueprint.toml:5: blueprint.requires_fastro: is {requires!r}, which doesn't include this fastro, 0.1.0.\n"
        f"  Install a fastro it accepts (uv tool install 'fastro{requires}'), then run this again."
    ]


@pytest.mark.parametrize(("requires", "tool"), [(">=0.1.0.dev0,<0.2", "0.1.0.dev0"), (">=0.1,<0.2", "0.1.1.dev3")])
def test_a_development_build_of_fastro_reads_a_catalog_whose_range_includes_it(requires: str, tool: str) -> None:
    assert _load(MINIMAL.replace(">=0.1.0.dev0", requires), Version(tool)).info.name == "mini"


def test_an_empty_requires_fastro_is_refused() -> None:
    assert _problems(MINIMAL.replace(">=0.1.0.dev0", "")) == [
        "blueprint.toml:5: blueprint.requires_fastro: should name the fastro versions that can read this catalog, "
        "such as '>=0.1.0.dev0,<0.2'."
    ]


@pytest.mark.parametrize("requires", ["===abc", "===0.1.0", ">=0.1,===abc"])
def test_arbitrary_equality_is_refused_in_requires_fastro(requires: str) -> None:
    assert _problems(MINIMAL.replace(">=0.1.0.dev0", requires)) == [
        f"blueprint.toml:5: blueprint.requires_fastro: {requires!r} uses '===', which can't express a version range: "
        "write it with ==, >= and <, such as '>=0.1.0.dev0,<0.2'."
    ]


def test_everything_cant_be_declared_as_a_verify_set() -> None:
    assert _problems(MINIMAL + '[verify.sets]\neverything = ["a"]\n') == [
        "blueprint.toml:7: verify.sets.everything: is always there, as the set of every feature.\n"
        "  Remove it from [verify.sets]."
    ]


def test_invalid_toml_is_refused_before_anything_else() -> None:
    assert _problems(MINIMAL + "presets = \n") == [
        "blueprint.toml:6: isn't valid TOML: Invalid value.\n  Fix the TOML on that line, then run this again."
    ]


NAME_RULE = "use lowercase letters, digits and underscores, starting with a letter."
LABEL_RULE = "use lowercase letters, digits, '-' and '_', starting with a letter."
REF = "a ref string or a table"
FEATURE = '[features.a]\nsummary = "A"\n'
OPTION = FEATURE + '[features.a.options.broker]\nchoices = ["redis"]\ndefault = "redis"\n'
CHECK = '[[verify.checks]]\nname = "x"\nrun = ["npm"]\n'


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ('nmae = "x"', "6: blueprint.nmae: isn't a key of [blueprint].\n  Did you mean 'name'?"),
        ("python_root = 1", "6: blueprint.python_root: should be a string, not an integer."),
        (
            'python_root = "/srv"',
            "6: blueprint.python_root: '/srv' isn't a repo path: it's absolute. Write it relative to the repo root, with '/'.",
        ),
        ('import_root = "my-src"', "6: blueprint.import_root: 'my-src' isn't an importable package name, such as 'src'."),
        ('blueprint_only = "docs/"', "6: blueprint.blueprint_only: should be a list of strings, not a string."),
        ('blueprint_only = ["docs/", 3]', "6: blueprint.blueprint_only[1]: should be a string, not an integer."),
        (
            'overlay = "../overlay/"',
            "6: blueprint.overlay: '../overlay/' isn't a repo path: it's climbing out with '..'. Write it "
            "relative to the repo root, with '/'.",
        ),
        ('extends = "mini"', "6: blueprint.extends: should be a table, not a string."),
        ('extends = { name = "mini" }', "6: blueprint.extends.version: is required.\n  Add version to [blueprint.extends]."),
        (
            'extends = { name = "mini", version = ">=0.1" }',
            "6: blueprint.extends.version: '>=0.1' isn't a PEP 440 version, such as '0.21.0'.",
        ),
        (
            'extends = { name = "Mini", version = "0.1.0" }',
            "6: blueprint.extends.name: 'Mini' isn't a valid blueprint name: use lowercase letters, digits "
            "and hyphens, starting with a letter.",
        ),
        (
            'extends = { name = "mini", version = "0.1.0", ref = "main" }',
            "6: blueprint.extends.ref: isn't a key of [blueprint.extends].\n  The keys of [blueprint.extends] "
            "are name, version.",
        ),
        ('dependencies = ["x"]', "6: blueprint.dependencies: should be a table, not a list."),
        (
            '[blueprint.dependencies]\nruntime = ["fast api"]',
            "7: blueprint.dependencies.runtime[0]: 'fast api' isn't a PEP 508 requirement, such as "
            "'fastapi[standard]>=0.115.8'.",
        ),
        (
            "[blueprint.dependencies]\ntest = []",
            "7: blueprint.dependencies.test: isn't a key of [blueprint.dependencies].\n  The keys of "
            "[blueprint.dependencies] are dev, runtime.",
        ),
        ("[preset]", "6: preset: isn't a top-level table.\n  Did you mean 'presets'?"),
        (
            "[presets]\nMinimal = []",
            "7: presets.Minimal: 'Minimal' isn't a valid preset name: use lowercase letters, digits, '-' and "
            "'_', starting with a letter.",
        ),
        ('[presets]\nminimal = "accounts"', "7: presets.minimal: should be a list of strings, not a string."),
        ('[presets]\nminimal = ["core"]', "7: presets.minimal[0]: 'core' is reserved for the files no feature owns."),
        ('[verify]\ninclude_presets = "yes"', "7: verify.include_presets: should be true or false, not a string."),
        ("[verify]\nsets = { all = 1 }", "7: verify.sets.all: should be a list of strings, not an integer."),
        (
            '[verify.sets]\n"Core only" = []',
            "7: verify.sets.\"Core only\": 'Core only' isn't a valid verify set name: use lowercase letters, "
            "digits, '-' and '_', starting with a letter.",
        ),
        (
            "[verify]\nmatrix = []",
            "7: verify.matrix: isn't a key of [verify].\n  The keys of [verify] are checks, include_presets, sets.",
        ),
        ("[verify]\nchecks = {}", "7: verify.checks: should be a list of tables, not a table."),
        ('[[verify.checks]]\nname = "frontend"', "6: verify.checks[0].run: is required.\n  Add run to [verify.checks[0]]."),
        (
            '[[verify.checks]]\nname = "x"\nrun = []',
            '8: verify.checks[0].run: should name the command to run, such as ["npm", "run", "build"].',
        ),
        (
            CHECK + 'cwd = "/srv"',
            "9: verify.checks[0].cwd: '/srv' isn't a repo path: it's absolute. Write it relative to the repo root, with '/'.",
        ),
        (
            CHECK + 'if = "a and"',
            "9: verify.checks[0].if: 'a and' isn't a condition: expected a feature name, 'not' or '(' at the end.",
        ),
        (
            CHECK + "env = {}",
            "9: verify.checks[0].env: isn't a key of [verify.checks[0]].\n  The keys of [verify.checks[0]] are "
            "cwd, if, name, run.",
        ),
        ('[points.routers]\ndoc = "x"', "6: points.routers.cardinality: is required.\n  Add cardinality to [points.routers]."),
        ('[points.routers]\ncardinality = "few"', "7: points.routers.cardinality: should be 'many' or 'one', not 'few'."),
        (
            '[points.Routers]\ncardinality = "many"',
            "6: points.Routers: 'Routers' isn't a valid name: use lowercase letters, digits and underscores, "
            "starting with a letter.",
        ),
        ('[points]\nrouters = "many"', "7: points.routers: should be a table, not a string."),
        ("[core]\nrequires = []", "7: core.requires: isn't a key of [core].\n  The keys of [core] are contributes."),
        (
            "[core.contributes]\nreadiness = [1]",
            "7: core.contributes.readiness[0]: should be a ref string or a table, not an integer.",
        ),
        ("[features.accounts]", "6: features.accounts.summary: is required.\n  Add summary to [features.accounts]."),
        (
            '[features.accounts]\nsummary = "A"\nrequire = ["x"]',
            "8: features.accounts.require: isn't a key of [features.accounts].\n  Did you mean 'requires'?",
        ),
        ('[features.app]\nsummary = "A"', "6: features.app: 'app' is reserved for the project's own code."),
        ('[features.or]\nsummary = "A"', "6: features.or: 'or' is reserved for the condition language."),
        ('[features.a]\nsummary = ["A"]', "7: features.a.summary: should be a string, not a list."),
        (FEATURE + "group = 1", "8: features.a.group: should be a string, not an integer."),
        (
            FEATURE + 'requires = ["Accounts"]',
            "8: features.a.requires[0]: 'Accounts' isn't a valid name: use lowercase letters, digits and "
            "underscores, starting with a letter.",
        ),
        (
            FEATURE + 'paths = ["a//b"]',
            "8: features.a.paths[0]: 'a//b' isn't a repo path: it's got an empty segment. Write it relative "
            "to the repo root, with '/'.",
        ),
        (
            FEATURE + 'dependencies = ["x y"]',
            "8: features.a.dependencies[0]: 'x y' isn't a PEP 508 requirement, such as 'fastapi[standard]>=0.115.8'.",
        ),
        (
            FEATURE + 'dev_dependencies = [">=1"]',
            "8: features.a.dev_dependencies[0]: '>=1' isn't a PEP 508 requirement, such as 'fastapi[standard]>=0.115.8'.",
        ),
        (FEATURE + "services = [1]", "8: features.a.services[0]: should be a string, not an integer."),
        (FEATURE + 'services = [""]', "8: features.a.services[0]: should name a compose service, not be empty."),
        (
            FEATURE + 'contributes = { routers = "m:x" }',
            "8: features.a.contributes.routers: should be a list of contributions, not a string.",
        ),
        (
            FEATURE + "contributes = { Routers = [] }",
            "8: features.a.contributes.Routers: 'Routers' isn't a valid name: use lowercase letters, digits "
            "and underscores, starting with a letter.",
        ),
        (
            FEATURE + "contributes = { r = [{ order = 1 }] }",
            "8: features.a.contributes.r[0]: needs one of ref, value and data.",
        ),
        (
            FEATURE + 'contributes = { r = [{ ref = "m:x", value = "x" }] }',
            "8: features.a.contributes.r[0]: takes only one of ref, value and data.",
        ),
        (
            FEATURE + "contributes = { r = [{ ref = 1 }] }",
            "8: features.a.contributes.r[0].ref: should be a string, not an integer.",
        ),
        (
            FEATURE + "contributes = { r = [{ value = true }] }",
            "8: features.a.contributes.r[0].value: should be a string, not a boolean.",
        ),
        (
            FEATURE + 'contributes = { r = [{ data = "x" }] }',
            "8: features.a.contributes.r[0].data: should be a table, not a string.",
        ),
        (
            FEATURE + 'contributes = { r = [{ ref = "m:x", order = "1" }] }',
            "8: features.a.contributes.r[0].order: should be an integer, not a string.",
        ),
        (
            FEATURE + 'contributes = { r = [{ ref = "m:x", order = 1.5 }] }',
            "8: features.a.contributes.r[0].order: should be an integer, not a float.",
        ),
        (
            FEATURE + 'contributes = { r = [{ ref = "m:x", if = "(" }] }',
            "8: features.a.contributes.r[0].if: '(' isn't a condition: expected a feature name, 'not' or '(' at the end.",
        ),
        (
            FEATURE + 'contributes = { r = [{ ref = "m:x", call = 1 }] }',
            "8: features.a.contributes.r[0].call: should be true or false, not an integer.",
        ),
        (
            FEATURE + 'contributes = { r = [{ ref = "m:x", prefix = ["/a"] }] }',
            "8: features.a.contributes.r[0].prefix: should be a string, an integer or true or false, not a list.",
        ),
        (FEATURE + "options = []", "8: features.a.options: should be a table, not a list."),
        (
            FEATURE + '[features.a.options.Broker]\nchoices = ["x"]\ndefault = "x"',
            "8: features.a.options.Broker: 'Broker' isn't a valid name: use lowercase letters, digits and "
            "underscores, starting with a letter.",
        ),
        (
            FEATURE + '[features.a.options.broker]\nchoices = ["redis"]',
            "8: features.a.options.broker.default: is required.\n  Add default to [features.a.options.broker].",
        ),
        (
            FEATURE + '[features.a.options.broker]\nchoices = []\ndefault = "x"',
            "9: features.a.options.broker.choices: should list at least one choice.",
        ),
        (
            FEATURE + '[features.a.options.broker]\nchoices = ["a b"]\ndefault = "a b"',
            "9: features.a.options.broker.choices[0]: 'a b' isn't a valid choice: use no whitespace, '(', ')' "
            "or '=', so a condition can name it.",
        ),
        (
            FEATURE + '[features.a.options.broker]\nchoices = ["redis", "redis"]\ndefault = "redis"',
            "9: features.a.options.broker.choices[1]: 'redis' is listed twice.",
        ),
        (
            FEATURE + '[features.a.options.broker]\nchoices = ["redis"]\ndefault = "kafka"',
            "10: features.a.options.broker.default: 'kafka' isn't one of the choices: 'redis'.",
        ),
        (
            OPTION + 'env = { "MY VAR" = "x" }',
            "11: features.a.options.broker.env.\"MY VAR\": 'MY VAR' isn't an environment variable name: use "
            "letters, digits and underscores, not starting with a digit.",
        ),
        (OPTION + "env = { VAR = 1 }", "11: features.a.options.broker.env.VAR: should be a string, not an integer."),
        (
            OPTION + 'services = { kafka = ["kafka"] }',
            "11: features.a.options.broker.services.kafka: 'kafka' isn't one of the choices: 'redis'.",
        ),
        (
            OPTION + 'dependencies = { redis = ["x y"] }',
            "11: features.a.options.broker.dependencies.redis[0]: 'x y' isn't a PEP 508 requirement, such as "
            "'fastapi[standard]>=0.115.8'.",
        ),
        (
            OPTION + 'help = "x"',
            "11: features.a.options.broker.help: isn't a key of [features.a.options.broker].\n  The keys of "
            "[features.a.options.broker] are choices, default, dependencies, env, services.",
        ),
        ('[features]\na = "x"', "7: features.a: should be a table, not a string."),
        (FEATURE + 'options = { broker = "redis" }', "8: features.a.options.broker: should be a table, not a string."),
        (
            FEATURE + 'contributes = { r = [{ ref = "not a ref!" }] }',
            "8: features.a.contributes.r[0].ref: 'not a ref!' isn't a ref: "
            "expected 'package.module:name' or 'package.module:name as alias'.",
        ),
        ('import_root = "class"', "6: blueprint.import_root: 'class' isn't an importable package name, such as 'src'."),
        (FEATURE + 'when = "x"', "8: features.a.when: should be a list of tables, not a string."),
        (
            FEATURE + "[[features.a.when]]\npaths = []",
            "8: features.a.when[0].if: is required.\n  Add if to [features.a.when[0]].",
        ),
        (
            FEATURE + '[[features.a.when]]\nif = "b"\nsummary = "x"',
            "10: features.a.when[0].summary: isn't a key of [features.a.when[0]].\n  The keys of "
            "[features.a.when[0]] are contributes, dependencies, dev_dependencies, if, paths, services.",
        ),
        (
            FEATURE + '[[features.a.when]]\nif = "b"\npaths = ["/x"]',
            "10: features.a.when[0].paths[0]: '/x' isn't a repo path: it's absolute. Write it relative to the "
            "repo root, with '/'.",
        ),
    ],
)
def test_a_key_that_breaks_its_rule_is_refused_at_its_line(body: str, problem: str) -> None:
    assert _problems(MINIMAL + body + "\n") == [f"blueprint.toml:{problem}"]


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ('name = "mini"', "name = 1", "2: blueprint.name: should be a string, not an integer."),
        (
            'name = "mini"',
            'name = "mini_bp"',
            "2: blueprint.name: 'mini_bp' isn't a valid blueprint name: "
            "use lowercase letters, digits and hyphens, starting with a letter.",
        ),
        (
            'version = "0.1.0"',
            'version = "latest"',
            "3: blueprint.version: 'latest' isn't a PEP 440 version, such as '0.21.0'.",
        ),
        ("schema = 1", 'schema = "1"', "4: blueprint.schema: should be an integer, not a string."),
        ("schema = 1", "schema = true", "4: blueprint.schema: should be an integer, not a boolean."),
        (
            'requires_fastro = ">=0.1.0.dev0"',
            'requires_fastro = "0.1"',
            "5: blueprint.requires_fastro: '0.1' isn't a PEP 440 version specifier, such as '>=0.20,<0.22'.",
        ),
    ],
)
def test_a_required_key_of_the_blueprint_table_that_breaks_its_rule_is_refused(old: str, new: str, problem: str) -> None:
    assert _problems(MINIMAL.replace(old, new)) == [f"blueprint.toml:{problem}"]
