"""Loading ``[generators]``, ``[commands]``, ``[plugin_api]`` and ``[checks]`` (spec 2.9, 2.11 and 2.12)."""

import pytest
from packaging.version import Version

from fastro.engine.blueprint import Blueprint, Command, Generator, Param, PluginApi, Register, load
from fastro.engine.problems import CatalogError
from fastro.engine.spec import RepoPath

MINIMAL = """\
[blueprint]
name = "mini"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"
"""

TOOLS = """\
[generators.deploy]
templates = "blueprint/templates/deploy"

[generators.deploy.params.api_port]
type = "int"
default = 8000
help = "The port the API listens on."

[generators.deploy.params.debug]
type = "bool"
default = false

[generators.deploy.params.domain]
type = "str"
default = "example.com"

[generators.deploy.params.proxy]
type = "choice"
default = "nginx"
choices = ["nginx", "caddy"]

[generators.deploy.params.internal_subnet]
type = "cidr"
default = "172.31.240.0/24"
private_only = true
max_prefix = 16

[generators.module]
templates = "blueprint/templates/module/"

[generators.module.register]
paths = ["backend/src/modules/{name}/", "backend/tests/integration/{name}/"]
contributes = { routers = [{ ref = "src.modules.{name}.routes:router as {name}_router", prefix = "/{name_dashed}" }] }

[generators.module.types]
str = { column = "String({length})", schema = "str", default_length = 255 }
int = { column = "Integer", schema = "int" }

[commands."env validate"]
module = "scripts.validate_env"

[commands."env secret"]
module = "scripts.gen_secret"
cwd = "backend/"
help = "Print a new secret."
outside_project = "token_hex"

[plugin_api]
version = 1
points = ["routers", "settings"]
public = ["src.infrastructure.composition", "src.plugins.*"]

[plugin_api.public_with]
accounts = ["src.modules.user.models"]

[checks.FR013]
allow = ["enums"]

[checks.toolchain.env]
DATABASE_URL = "sqlite://"
"""


def _load(text: str) -> Blueprint:
    return load("blueprint.toml", text, Version("0.1.0"))


def _problems(text: str) -> list[str]:
    with pytest.raises(CatalogError) as caught:
        _load(text)
    return [str(problem) for problem in caught.value.problems]


def test_without_these_sections_there_are_no_generators_commands_api_or_checks() -> None:
    blueprint = _load(MINIMAL)

    assert (blueprint.generators, blueprint.commands, blueprint.plugin_api) == ({}, {}, None)
    assert (blueprint.checks, blueprint.toolchain_env) == ({}, {})


def test_generators_are_read_with_their_params() -> None:
    deploy = _load(MINIMAL + TOOLS).generators["deploy"]
    params = ("generators", "deploy", "params")

    assert deploy == Generator(
        name="deploy",
        templates=RepoPath("blueprint/templates/deploy"),
        params={
            "api_port": Param("api_port", "int", 8000, help="The port the API listens on.", at=(*params, "api_port")),
            "debug": Param("debug", "bool", False, at=(*params, "debug")),
            "domain": Param("domain", "str", "example.com", at=(*params, "domain")),
            "proxy": Param("proxy", "choice", "nginx", choices=("nginx", "caddy"), at=(*params, "proxy")),
            "internal_subnet": Param(
                "internal_subnet", "cidr", "172.31.240.0/24", private_only=True, max_prefix=16, at=(*params, "internal_subnet")
            ),
        },
        register=None,
        types={},
        at=("generators", "deploy"),
    )


def test_the_module_generator_also_has_register_and_types() -> None:
    module = _load(MINIMAL + TOOLS).generators["module"]

    assert module.templates == RepoPath("blueprint/templates/module/")
    assert module.register == Register(
        paths=("backend/src/modules/{name}/", "backend/tests/integration/{name}/"),
        contributes={"routers": ({"ref": "src.modules.{name}.routes:router as {name}_router", "prefix": "/{name_dashed}"},)},
    )
    assert module.types == {
        "str": {"column": "String({length})", "schema": "str", "default_length": 255},
        "int": {"column": "Integer", "schema": "int"},
    }


@pytest.mark.parametrize(
    ("default", "max_prefix"),
    [
        ("172.31.240.0/24", "16"),
        ("10.0.0.0/16", "16"),
        ("10.0.0.1/32", "32"),
        ("fd00::/64", "64"),
        ("fd00::/64", "24"),
        ("0.0.0.0/0", "0"),
    ],
)
def test_a_cidr_default_as_narrow_as_max_prefix_or_narrower_loads(default: str, max_prefix: str) -> None:
    body = PARAM + f'type = "cidr"\ndefault = "{default}"\nmax_prefix = {max_prefix}\n'

    assert _load(MINIMAL + body).generators["deploy"].params["p"].max_prefix == int(max_prefix)


def test_without_max_prefix_any_cidr_default_loads() -> None:
    body = PARAM + 'type = "cidr"\ndefault = "10.0.0.0/8"\n'

    assert _load(MINIMAL + body).generators["deploy"].params["p"].default == "10.0.0.0/8"


@pytest.mark.parametrize(
    "ref",
    [
        "src.modules.{name}.models:{Name}",
        "src.modules.{name}.routes:router as {name}_router",
        "src.modules.{name}.schemas:{Name}Read as {name}_read",
    ],
)
def test_a_register_ref_may_use_its_name_fields_where_python_takes_an_identifier(ref: str) -> None:
    module = _load(MINIMAL + MODULE + f'contributes = {{ models = [{{ ref = "{ref}" }}] }}\n').generators["module"]

    assert module.register == Register(paths=(), contributes={"models": ({"ref": ref},)})


def test_commands_are_read_by_their_words() -> None:
    commands = _load(MINIMAL + TOOLS).commands

    assert commands == {
        ("env", "validate"): Command(("env", "validate"), "scripts.validate_env", at=("commands", "env validate")),
        ("env", "secret"): Command(
            ("env", "secret"),
            "scripts.gen_secret",
            cwd=RepoPath("backend/"),
            help="Print a new secret.",
            outside_project="token_hex",
            at=("commands", "env secret"),
        ),
    }


ACCOUNTS = '[features.accounts]\nsummary = "Users and login"\n'


def test_a_feature_declares_its_own_commands_apart_from_the_top_level_ones() -> None:
    body = ACCOUNTS + '[features.accounts.commands."accounts unlock"]\nmodule = "scripts.unlock_account"\nhelp = "Unlock."\n'
    catalog = _load(MINIMAL + body)

    assert catalog.features[0].commands == {
        ("accounts", "unlock"): Command(
            ("accounts", "unlock"),
            "scripts.unlock_account",
            help="Unlock.",
            at=("features", "accounts", "commands", "accounts unlock"),
        )
    }
    assert catalog.commands == {}


def test_a_feature_without_commands_has_none() -> None:
    assert _load(MINIMAL + ACCOUNTS).features[0].commands == {}


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (
            '[commands."db seed"]\nmodule = "a"\n' + ACCOUNTS + '[features.accounts.commands."db seed"]\nmodule = "b"\n',
            '10: features.accounts.commands."db seed": \'db seed\' is declared by commands."db seed" already.',
        ),
        (
            ACCOUNTS + '[features.accounts.commands."db seed"]\nmodule = "b"\n[commands."db seed"]\nmodule = "a"\n',
            '10: commands."db seed": \'db seed\' is declared by features.accounts.commands."db seed" already.',
        ),
        (
            ACCOUNTS
            + '[features.accounts.commands."db seed"]\nmodule = "a"\n'
            + '[features.tiers]\nsummary = "Tiers"\n[features.tiers.commands."db seed"]\nmodule = "b"\n',
            '12: features.tiers.commands."db seed": \'db seed\' is declared by features.accounts.commands."db seed" already.',
        ),
        (
            ACCOUNTS + '[features.accounts.commands."Unlock"]\nmodule = "m"\n',
            "8: features.accounts.commands.Unlock: 'Unlock' isn't a command: write its words in lowercase letters, "
            "digits and hyphens, one space apart, such as 'env validate'.",
        ),
        (
            ACCOUNTS + '[features.accounts.commands."accounts unlock"]\nhelp = "Unlock."\n',
            '8: features.accounts.commands."accounts unlock".module: is required.\n'
            '  Add module to [features.accounts.commands."accounts unlock"].',
        ),
        (
            ACCOUNTS + "[features.accounts.commands]\nunlock = 1\n",
            "9: features.accounts.commands.unlock: should be a table, not an integer.",
        ),
        (
            ACCOUNTS + '[[features.accounts.when]]\nif = "taskiq"\ncommands = {}\n',
            "10: features.accounts.when[0].commands: isn't a key of [features.accounts.when[0]].\n"
            "  The keys of [features.accounts.when[0]] are contributes, dependencies, dev_dependencies, if, "
            "paths, services.",
        ),
    ],
)
def test_a_command_declared_twice_or_badly_in_a_feature_is_refused(body: str, problem: str) -> None:
    assert _problems(MINIMAL + body) == [f"blueprint.toml:{problem}"]


def test_the_plugin_api_is_read() -> None:
    assert _load(MINIMAL + TOOLS).plugin_api == PluginApi(
        version=1,
        points=("routers", "settings"),
        public=("src.infrastructure.composition", "src.plugins.*"),
        public_with={"accounts": ("src.modules.user.models",)},
        at=("plugin_api",),
    )


def test_checks_are_read_as_free_form_tables_with_the_toolchain_env_apart() -> None:
    blueprint = _load(MINIMAL + TOOLS)

    assert blueprint.checks == {"FR013": {"allow": ["enums"]}}
    assert blueprint.toolchain_env == {"DATABASE_URL": "sqlite://"}


DEPLOY = '[generators.deploy]\ntemplates = "blueprint/templates/deploy"\n'
PARAM = DEPLOY + "[generators.deploy.params.p]\n"
MODULE = '[generators.module]\ntemplates = "blueprint/templates/module"\n[generators.module.register]\n'
PATTERN = "isn't a module, or a package followed by '.*', such as 'src.infrastructure.composition'."


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("[generators.deploy]\n", "6: generators.deploy.templates: is required.\n  Add templates to [generators.deploy]."),
        (
            '[generators.Deploy]\ntemplates = "t"\n',
            "6: generators.Deploy: 'Deploy' isn't a valid generator name: use lowercase letters, digits and hyphens, "
            "starting with a letter.",
        ),
        (
            DEPLOY.replace('"blueprint/templates/deploy"', '"/srv/t"'),
            "7: generators.deploy.templates: '/srv/t' isn't a repo "
            "path: it's absolute. Write it relative to the repo root, with '/'.",
        ),
        (
            DEPLOY + "modes = []\n",
            "8: generators.deploy.modes: isn't a key of [generators.deploy].\n"
            "  The keys of [generators.deploy] are params, register, templates, types.",
        ),
        (
            DEPLOY + "[generators.deploy.register]\npaths = []\n",
            "8: generators.deploy.register: is only for the module generator, which make module runs.",
        ),
        (
            DEPLOY + "[generators.deploy.types]\n",
            "8: generators.deploy.types: is only for the module generator, which make module runs.",
        ),
        (
            PARAM + "default = 1\n",
            "8: generators.deploy.params.p.type: is required.\n  Add type to [generators.deploy.params.p].",
        ),
        (
            PARAM + 'type = "int"\n',
            "8: generators.deploy.params.p.default: is required.\n  Add default to [generators.deploy.params.p].",
        ),
        (
            PARAM + 'type = "float"\ndefault = 1.0\n',
            "9: generators.deploy.params.p.type: should be one of bool, choice, cidr, int, str, not 'float'.",
        ),
        (
            PARAM + 'type = "int"\ndefault = "8000"\n',
            "10: generators.deploy.params.p.default: should be an integer, not a string.",
        ),
        (
            PARAM + 'type = "int"\ndefault = true\n',
            "10: generators.deploy.params.p.default: should be an integer, not a boolean.",
        ),
        (
            PARAM + 'type = "bool"\ndefault = 1\n',
            "10: generators.deploy.params.p.default: should be true or false, not an integer.",
        ),
        (PARAM + 'type = "str"\ndefault = 1\n', "10: generators.deploy.params.p.default: should be a string, not an integer."),
        (
            PARAM + 'type = "int"\ndefault = 1\nhelp = 2\n',
            "11: generators.deploy.params.p.help: should be a string, not an integer.",
        ),
        (
            PARAM + 'type = "choice"\ndefault = "a"\n',
            "8: generators.deploy.params.p.choices: is required for a choice param.\n"
            "  Add choices to [generators.deploy.params.p].",
        ),
        (
            PARAM + 'type = "choice"\ndefault = "c"\nchoices = ["a", "b"]\n',
            "10: generators.deploy.params.p.default: 'c' isn't one of the choices: 'a', 'b'.",
        ),
        (
            PARAM + 'type = "choice"\ndefault = "a"\nchoices = ["a", "a"]\n',
            "11: generators.deploy.params.p.choices[1]: 'a' is listed twice.",
        ),
        (
            PARAM + 'type = "int"\ndefault = 1\nchoices = ["a"]\n',
            "11: generators.deploy.params.p.choices: is only for a choice param.",
        ),
        (
            PARAM + 'type = "str"\ndefault = "x"\nprivate_only = true\n',
            "11: generators.deploy.params.p.private_only: is only for a cidr param.",
        ),
        (
            PARAM + 'type = "str"\ndefault = "x"\nmax_prefix = 8\n',
            "11: generators.deploy.params.p.max_prefix: is only for a cidr param.",
        ),
        (
            PARAM + 'type = "cidr"\ndefault = "172.31.240.1/24"\n',
            "10: generators.deploy.params.p.default: '172.31.240.1/24' isn't a network address, such as '172.31.240.0/24'.",
        ),
        (
            PARAM + 'type = "cidr"\ndefault = "8.8.8.0/24"\nprivate_only = true\n',
            "10: generators.deploy.params.p.default: '8.8.8.0/24' isn't a private network, and private_only is set.",
        ),
        (
            PARAM + 'type = "cidr"\ndefault = "10.0.0.0/8"\nmax_prefix = 129\n',
            "11: generators.deploy.params.p.max_prefix: should be from 0 to 128, not 129.",
        ),
        (
            PARAM + 'type = "cidr"\ndefault = "10.0.0.0/8"\nmax_prefix = 16\n',
            "10: generators.deploy.params.p.default: '10.0.0.0/8' is wider than max_prefix allows: it's /8, "
            "and this param takes /16 or longer.",
        ),
        (
            PARAM + 'type = "cidr"\ndefault = "fd00::/48"\nmax_prefix = 64\n',
            "10: generators.deploy.params.p.default: 'fd00::/48' is wider than max_prefix allows: it's /48, "
            "and this param takes /64 or longer.",
        ),
        (
            PARAM + 'type = "cidr"\ndefault = "10.0.0.0/24"\nmax_prefix = 33\n',
            "10: generators.deploy.params.p.default: '10.0.0.0/24' is an IPv4 network, and max_prefix = 33 needs "
            "an IPv6 one, since IPv4 prefixes stop at /32.",
        ),
        (
            PARAM + 'type = "cidr"\ndefault = "10.0.0.0/8"\nprivate_only = "yes"\n',
            "11: generators.deploy.params.p.private_only: should be true or false, not a string.",
        ),
        (
            PARAM.replace(".p]", ".Port]") + 'type = "int"\ndefault = 1\n',
            "8: generators.deploy.params.Port: 'Port' isn't a valid name: use lowercase letters, digits and underscores, "
            "starting with a letter.",
        ),
        (
            MODULE + 'paths = ["backend/src/modules/{nam}/"]\n',
            "9: generators.module.register.paths[0]: 'backend/src/modules/{nam}/' uses {nam}, "
            "and register templates take {name}, {name_dashed} and {Name}.",
        ),
        (
            MODULE + 'paths = ["../{name}/"]\n',
            "9: generators.module.register.paths[0]: '../{name}/' isn't a repo path: it's climbing out with '..'. "
            "Write it relative to the repo root, with '/'.",
        ),
        (
            MODULE + 'paths = ["backend/{name"]\n',
            "9: generators.module.register.paths[0]: 'backend/{name' isn't a format string: expected '}' before end of string.",
        ),
        (
            MODULE + 'contributes = { routers = [{ ref = "src.modules.{name}.routes" }] }\n',
            "9: generators.module.register.contributes.routers[0].ref: 'src.modules.{name}.routes' isn't a ref: "
            "expected 'package.module:name' or 'package.module:name as alias'.",
        ),
        (
            MODULE + 'contributes = { routers = [{ ref = "src.modules.{name_dashed}.routes:router" }] }\n',
            "9: generators.module.register.contributes.routers[0].ref: 'src.modules.{name_dashed}.routes:router' "
            "isn't a ref: expected 'package.module:name' or 'package.module:name as alias'.",
        ),
        (
            MODULE + 'contributes = { routers = [{ ref = "m:r", prefix = "/{Nme}" }] }\n',
            "9: generators.module.register.contributes.routers[0].prefix: '/{Nme}' uses {Nme}, "
            "and register templates take {name}, {name_dashed} and {Name}.",
        ),
        (
            MODULE + "contributes = { routers = [{ order = 1 }] }\n",
            "9: generators.module.register.contributes.routers[0]: needs one of ref, value and data.",
        ),
        (
            MODULE + 'paths = []\nmodule = "x"\n',
            "10: generators.module.register.module: isn't a key of [generators.module.register].\n"
            "  The keys of [generators.module.register] are contributes, paths.",
        ),
        (
            '[generators.module]\ntemplates = "t"\n[generators.module.types]\nstr = "String"\n',
            "9: generators.module.types.str: should be a table, not a string.",
        ),
        ("[generators]\ndeploy = 1\n", "7: generators.deploy: should be a table, not an integer."),
        (DEPLOY + "params = { p = 1 }\n", "8: generators.deploy.params.p: should be a table, not an integer."),
        (
            '[generators.module]\ntemplates = "t"\nregister = 1\n',
            "8: generators.module.register: should be a table, not an integer.",
        ),
        (
            MODULE + 'contributes = { routers = "m:r" }\n',
            "9: generators.module.register.contributes.routers: should be a list of contributions, not a string.",
        ),
        (
            MODULE + 'contributes = { routers = ["src.{nme}:r"] }\n',
            "9: generators.module.register.contributes.routers[0]: 'src.{nme}:r' uses {nme}, "
            "and register templates take {name}, {name_dashed} and {Name}.",
        ),
        (
            MODULE + "contributes = { routers = [1] }\n",
            "9: generators.module.register.contributes.routers[0]: should be a ref string or a table, not an integer.",
        ),
        ("[commands]\nenv = 1\n", "7: commands.env: should be a table, not an integer."),
        (
            '[commands."env validate"]\n',
            '6: commands."env validate".module: is required.\n  Add module to [commands."env validate"].',
        ),
        (
            '[commands."env  validate"]\nmodule = "m"\n',
            "6: commands.\"env  validate\": 'env  validate' isn't a command: write its words in lowercase letters, "
            "digits and hyphens, one space apart, such as 'env validate'.",
        ),
        (
            '[commands.Env]\nmodule = "m"\n',
            "6: commands.Env: 'Env' isn't a command: write its words in lowercase letters, digits and hyphens, "
            "one space apart, such as 'env validate'.",
        ),
        (
            '[commands.env]\nmodule = "scripts.validate-env"\n',
            "7: commands.env.module: 'scripts.validate-env' isn't a module name, such as 'scripts.validate_env'.",
        ),
        (
            '[commands.env]\nmodule = "class.x"\n',
            "7: commands.env.module: 'class.x' isn't a module name, such as 'scripts.validate_env'.",
        ),
        (
            '[commands.env]\nmodule = "m"\ncwd = "/srv"\n',
            "8: commands.env.cwd: '/srv' isn't a repo path: it's absolute. Write it relative to the repo root, with '/'.",
        ),
        ('[commands.env]\nmodule = "m"\nhelp = 1\n', "8: commands.env.help: should be a string, not an integer."),
        (
            '[commands.env]\nmodule = "m"\noutside_project = "ignore"\n',
            "8: commands.env.outside_project: should be 'error' or 'token_hex', not 'ignore'.",
        ),
        (
            '[commands.env]\nmodule = "m"\nargs = []\n',
            "8: commands.env.args: isn't a key of [commands.env].\n"
            "  The keys of [commands.env] are cwd, help, module, outside_project.",
        ),
        ("[plugin_api]\npoints = []\n", "6: plugin_api.version: is required.\n  Add version to [plugin_api]."),
        ('[plugin_api]\nversion = "1"\n', "7: plugin_api.version: should be an integer, not a string."),
        ("[plugin_api]\nversion = 0\n", "7: plugin_api.version: should be 1 or more, not 0."),
        (
            '[plugin_api]\nversion = 1\npoints = ["Routers"]\n',
            "8: plugin_api.points[0]: 'Routers' isn't a valid name: use lowercase letters, digits and underscores, "
            "starting with a letter.",
        ),
        ('[plugin_api]\nversion = 1\npublic = ["src.*.x"]\n', f"8: plugin_api.public[0]: 'src.*.x' {PATTERN}"),
        ('[plugin_api]\nversion = 1\npublic = ["src..x"]\n', f"8: plugin_api.public[0]: 'src..x' {PATTERN}"),
        ('[plugin_api]\nversion = 1\npublic = ["*"]\n', f"8: plugin_api.public[0]: '*' {PATTERN}"),
        (
            '[plugin_api]\nversion = 1\n[plugin_api.public_with]\nAccounts = ["m"]\n',
            "9: plugin_api.public_with.Accounts: 'Accounts' isn't a valid name: use lowercase letters, digits and "
            "underscores, starting with a letter.",
        ),
        (
            '[plugin_api]\nversion = 1\n[plugin_api.public_with]\naccounts = ["m-x"]\n',
            f"9: plugin_api.public_with.accounts[0]: 'm-x' {PATTERN}",
        ),
        (
            "[plugin_api]\nversion = 1\nprivate = []\n",
            "8: plugin_api.private: isn't a key of [plugin_api].\n"
            "  The keys of [plugin_api] are points, public, public_with, version.",
        ),
        (
            "[checks.fr013]\nallow = []\n",
            "6: checks.fr013: 'fr013' isn't a check code, such as 'FR013'.",
        ),
        ("[checks]\nFR013 = 1\n", "7: checks.FR013: should be a table, not an integer."),
        (
            "[checks.toolchain]\npython = '3.12'\n",
            "7: checks.toolchain.python: isn't a key of [checks.toolchain].\n  The keys of [checks.toolchain] are env.",
        ),
        ("[checks.toolchain.env]\nPORT = 8000\n", "7: checks.toolchain.env.PORT: should be a string, not an integer."),
        (
            "[checks.toolchain.env]\n\"MY VAR\" = 'x'\n",
            "7: checks.toolchain.env.\"MY VAR\": 'MY VAR' isn't an environment variable name: use letters, digits "
            "and underscores, not starting with a digit.",
        ),
    ],
)
def test_a_key_that_breaks_its_rule_is_refused_at_its_line(body: str, problem: str) -> None:
    assert _problems(MINIMAL + body) == [f"blueprint.toml:{problem}"]


def test_a_plugin_api_that_isnt_a_table_is_refused() -> None:
    assert _problems("plugin_api = 1\n" + MINIMAL) == ["blueprint.toml:1: plugin_api: should be a table, not an integer."]
