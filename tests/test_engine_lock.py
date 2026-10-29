"""Loading a project's ``.fastro/lock.toml`` (spec 4), key by key."""

import pytest
from packaging.version import Version

from fastro.engine import project
from fastro.engine.blueprint import CrossReferenceError, IncompatibleCatalog
from fastro.engine.lock import Lock, LockedFile, LockedPlugin, Provenance, load
from fastro.engine.problems import CatalogError
from fastro.engine.spec import RepoPath

SHA1 = "3f9c2e1" + "0" * 33
SHA256 = "ab" * 32

FULL = f"""\
[lock]
schema = 1
fastro = "0.1.0"
blueprint = {{ name = "fastro", version = "0.21.0", source = "gh:benavlabs/FastAPI-boilerplate@v0.21.0", commit = "{SHA1}" }}
features = ["accounts", "admin", "cache"]
options = {{ "cache.backend" = "redis" }}
starter = {{ name = "b2b-api", version = "0.1.0", source = "gh:benavlabs/fastro-starters@v0.1.0#b2b-api", commit = "{SHA1}" }}

[plugins.stripe]
source = "gh:acme/fastro-stripe@v1.2.0"
version = "1.2.0"
commit = "{SHA256}"

[files]
"backend/src/wiring/app.py" = {{ sha256 = "{SHA256}", origin = "wiring" }}
"backend/src/modules/tier/models.py" = {{ sha256 = "{SHA256}", origin = "feature:tiers" }}
"backend/src/plugins/stripe/routes.py" = {{ sha256 = "{SHA256}", origin = "plugin:stripe", linked = true }}
"README.md" = {{ sha256 = "{SHA256}", origin = "overlay" }}
"""


def _text(lock: str = "", tables: str = "") -> str:
    return (
        '[lock]\nschema = 1\nfastro = "0.1.0"\n'
        'blueprint = { name = "fastro", version = "0.21.0", source = "../FastAPI-boilerplate" }\n'
        f"features = []\n{lock}{tables}"
    )


def _problems(text: str) -> list[str]:
    with pytest.raises(CatalogError) as caught:
        load("lock.toml", text)
    return [str(problem) for problem in caught.value.problems]


def test_every_key_is_read_and_files_need_not_be_sorted() -> None:
    lock = load("lock.toml", FULL)

    assert lock == Lock(
        schema=1,
        fastro=Version("0.1.0"),
        blueprint=Provenance("fastro", Version("0.21.0"), "gh:benavlabs/FastAPI-boilerplate@v0.21.0", SHA1),
        features=("accounts", "admin", "cache"),
        options={("cache", "backend"): "redis"},
        starter=Provenance("b2b-api", Version("0.1.0"), "gh:benavlabs/fastro-starters@v0.1.0#b2b-api", SHA1),
        plugins={"stripe": LockedPlugin("stripe", "gh:acme/fastro-stripe@v1.2.0", Version("1.2.0"), SHA256)},
        files={
            RepoPath("backend/src/wiring/app.py"): LockedFile(SHA256, "wiring"),
            RepoPath("backend/src/modules/tier/models.py"): LockedFile(SHA256, "feature:tiers"),
            RepoPath("backend/src/plugins/stripe/routes.py"): LockedFile(SHA256, "plugin:stripe", linked=True),
            RepoPath("README.md"): LockedFile(SHA256, "overlay"),
        },
        document=lock.document,
    )


def test_a_lock_with_only_the_required_keys_gets_the_defaults() -> None:
    lock = load("lock.toml", _text())

    assert (lock.blueprint.commit, lock.options, lock.starter, lock.plugins, lock.files) == (None, {}, None, {}, {})


@pytest.mark.parametrize(
    "source",
    ["/home/me/fastro", "./fastro", "../fastro#blueprint"],
)
def test_a_path_source_may_carry_a_commit_or_not(source: str) -> None:
    text = _text().replace('"../FastAPI-boilerplate"', f'"{source}", commit = "{SHA1}"')

    assert load("lock.toml", text).blueprint.commit == SHA1


@pytest.mark.parametrize(
    "origin",
    ["core", "wiring", "overlay", "deps", "env", "feature:tier_limits", "plugin:stripe", "make:module"],
)
def test_every_origin_form_is_read(origin: str) -> None:
    text = _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "{origin}" }}\n')

    assert load("lock.toml", text).files[RepoPath("a.py")].origin == origin


def test_a_schema_newer_than_this_fastro_is_refused_as_incompatible() -> None:
    with pytest.raises(IncompatibleCatalog) as caught:
        load("lock.toml", _text().replace("schema = 1", "schema = 2"))

    assert [str(problem) for problem in caught.value.problems] == [
        "lock.toml:2: lock.schema: is 2, which this fastro doesn't read.\n"
        "  Upgrade fastro (uv tool upgrade fastro), then run this again."
    ]


COMMIT_FIX = "  Add commit, the full git commit that source resolved to."


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            '[files]\n"a.py" = { sha256 = "x", origin = "core" }\n',
            "1: lock: is required.\n  Add a [lock] table: fastro writes it when it makes or changes a project.",
        ),
        ("lock = 1\n", "1: lock: should be a table, not an integer."),
        (
            _text().replace('{ name = "fastro", version = "0.21.0", source = "../FastAPI-boilerplate" }', '"fastro"'),
            "4: lock.blueprint: should be a table, not a string.",
        ),
        (_text(tables='[plugins]\nstripe = "../stripe"\n'), "7: plugins.stripe: should be a table, not a string."),
        (_text().replace("schema = 1\n", ""), "1: lock.schema: is required.\n  Add schema to [lock]."),
        (_text().replace("schema = 1", "schema = 0"), "2: lock.schema: should be 1, not 0."),
        (_text().replace("schema = 1", 'schema = "1"'), "2: lock.schema: should be an integer, not a string."),
        (
            _text().replace('fastro = "0.1.0"', 'fastro = "latest"'),
            "3: lock.fastro: 'latest' isn't a PEP 440 version, such as '0.21.0'.",
        ),
        (_text("feature = []\n"), "6: lock.feature: isn't a key of [lock].\n  Did you mean 'features'?"),
        (
            _text(tables="[extra]\n"),
            "6: extra: isn't a top-level key of lock.toml.\n  The top-level keys are files, lock, plugins.",
        ),
        (
            _text().replace('"../FastAPI-boilerplate"', '"gh:benavlabs/FastAPI-boilerplate@v0.21.0"'),
            "4: lock.blueprint.commit: is required for a gh: or git+ source.\n" + COMMIT_FIX,
        ),
        (
            _text().replace('"../FastAPI-boilerplate"', f'"fastro", commit = "{SHA1}"'),
            "4: lock.blueprint.commit: is only for a gh:, git+ or path source.",
        ),
        (
            _text().replace('"../FastAPI-boilerplate"', '"../fastro", commit = "3f9c2e1"'),
            "4: lock.blueprint.commit: '3f9c2e1' isn't a full git commit: write its 40 or 64 lowercase hex characters.",
        ),
        (
            _text().replace('"../FastAPI-boilerplate"', f'"../fastro", commit = "{SHA1.upper()}"'),
            f"4: lock.blueprint.commit: '{SHA1.upper()}' isn't a full git commit: write its 40 or 64 lowercase hex characters.",
        ),
        (
            _text().replace('version = "0.21.0"', 'version = ">=0.21"'),
            "4: lock.blueprint.version: '>=0.21' isn't a PEP 440 version, such as '0.21.0'.",
        ),
        (
            _text().replace(', source = "../FastAPI-boilerplate"', ""),
            "4: lock.blueprint.source: is required.\n  Add source to [lock.blueprint].",
        ),
        (
            _text().replace("features = []", 'features = ["cache", "cache"]'),
            "5: lock.features[1]: 'cache' is listed twice.",
        ),
        (
            _text("[lock.options]\ncache.backend = 'redis'\n"),
            "7: lock.options.cache: should be a string, not a table.\n"
            '  Quote the whole key, such as "cache.backend" = "redis".',
        ),
        (
            _text('starter = { name = "b2b_api", version = "0.1.0", source = "../starter" }\n'),
            "6: lock.starter.name: 'b2b_api' isn't a valid starter name: use lowercase letters, digits and hyphens, "
            "starting with a letter.",
        ),
        (
            _text(tables='[plugins.stripe]\nsource = "../stripe"\nversion = ">=1"\n'),
            "8: plugins.stripe.version: '>=1' isn't a PEP 440 version, such as '0.21.0'.",
        ),
        (
            _text(tables='[plugins.stripe]\nsource = "gh:acme/fastro-stripe@v1.2.0"\nversion = "1.2.0"\n'),
            "6: plugins.stripe.commit: is required for a gh: or git+ source.\n" + COMMIT_FIX,
        ),
        (
            _text(tables='[plugins.Stripe]\nsource = "../stripe"\nversion = "1.2.0"\n'),
            "6: plugins.Stripe: 'Stripe' isn't a valid name: use lowercase letters, digits and underscores, "
            "starting with a letter.",
        ),
        (
            _text(tables=f'[files]\n"backend/" = {{ sha256 = "{SHA256}", origin = "core" }}\n'),
            "7: files.\"backend/\": 'backend/' names a directory, and [files] lists files.",
        ),
        (
            _text(tables=f'[files]\n"../a.py" = {{ sha256 = "{SHA256}", origin = "core" }}\n'),
            "7: files.\"../a.py\": '../a.py' isn't a repo path: it's climbing out with '..'. "
            "Write it relative to the repo root, with '/'.",
        ),
        (
            _text(tables='[files]\n"a.py" = { sha256 = "abc", origin = "core" }\n'),
            "7: files.\"a.py\".sha256: 'abc' isn't a sha256: write its 64 lowercase hex characters.",
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "feat:tiers" }}\n'),
            "7: files.\"a.py\".origin: 'feat:tiers' isn't an origin: use core, wiring, overlay, deps, env, "
            "feature:<name>, plugin:<name> or make:<generator>.",
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "feature:Tiers" }}\n'),
            "7: files.\"a.py\".origin: 'feature:Tiers' isn't an origin: 'Tiers' isn't a valid name: use lowercase "
            "letters, digits and underscores, starting with a letter.",
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "make:Module" }}\n'),
            "7: files.\"a.py\".origin: 'make:Module' isn't an origin: 'Module' isn't a valid generator name: use "
            "lowercase letters, digits and hyphens, starting with a letter.",
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "feature:core" }}\n'),
            "7: files.\"a.py\".origin: 'feature:core' isn't an origin: 'core' is reserved for the files no feature owns.",
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "plugin:app" }}\n'),
            "7: files.\"a.py\".origin: 'plugin:app' isn't an origin: 'app' is reserved for the project's own code.",
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "feature:and" }}\n'),
            "7: files.\"a.py\".origin: 'feature:and' isn't an origin: 'and' is reserved for the condition language.",
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "core:x" }}\n'),
            "7: files.\"a.py\".origin: 'core:x' isn't an origin: use core, wiring, overlay, deps, env, "
            "feature:<name>, plugin:<name> or make:<generator>.",
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "wiring", linked = true }}\n'),
            '7: files."a.py".linked: is only for a plugin:<name> origin.',
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "plugin:stripe", linked = "yes" }}\n'),
            '7: files."a.py".linked: should be true or false, not a string.',
        ),
        (
            _text(tables='[files]\n"a.py" = { origin = "core" }\n'),
            '7: files."a.py".sha256: is required.\n  Add sha256 to [files."a.py"].',
        ),
        (
            _text(tables=f'[files]\n"a.py" = {{ sha256 = "{SHA256}", origin = "core", mode = 1 }}\n'),
            '7: files."a.py".mode: isn\'t a key of [files."a.py"].\n  The keys of [files."a.py"] are linked, origin, sha256.',
        ),
        (_text(tables='[files]\n"a.py" = "core"\n'), '7: files."a.py": should be a table, not a string.'),
    ],
)
def test_a_key_that_breaks_its_rule_is_refused_at_its_line(text: str, problem: str) -> None:
    assert _problems(text) == [f"lock.toml:{problem}"]


PROJECT = """\
blueprint = { name = "fastro", version = "0.21.0" }
features = []

[project]
name = "api"

[plugins]
stripe = "gh:acme/fastro-stripe"
"""

STRIPE = '[plugins.stripe]\nsource = "../stripe"\nversion = "1.2.0"\n'
PIN_FIX = "  fastro writes both together, so one was edited by hand. Make them agree with .fastro/blueprint.toml."


def _against(lock: str, fastro: str = PROJECT) -> CatalogError:
    with pytest.raises(CatalogError) as caught:
        load("lock.toml", lock, project.load("fastro.toml", fastro))
    return caught.value


def test_a_lock_that_agrees_with_fastro_toml_loads() -> None:
    assert list(load("lock.toml", _text(tables=STRIPE), project.load("fastro.toml", PROJECT)).plugins) == ["stripe"]


@pytest.mark.parametrize(
    ("lock", "fastro", "problem"),
    [
        (
            _text(tables=STRIPE).replace('name = "fastro"', 'name = "fastroai"'),
            PROJECT,
            "lock.toml:4: lock.blueprint.name: is 'fastroai', and fastro.toml pins 'fastro'.\n" + PIN_FIX,
        ),
        (
            _text(tables=STRIPE).replace('version = "0.21.0"', 'version = "0.20.0"'),
            PROJECT,
            "lock.toml:4: lock.blueprint.version: is 0.20.0, and fastro.toml pins 0.21.0.\n" + PIN_FIX,
        ),
        (
            _text(tables=STRIPE).replace('version = "0.21.0"', 'version = "0.20.0"'),
            "source = true\n" + PROJECT,
            "lock.toml:4: lock.blueprint.version: is 0.20.0, and fastro.toml pins 0.21.0.\n"
            + PIN_FIX.replace(".fastro/blueprint.toml", "blueprint.toml"),
        ),
        (
            _text(tables=STRIPE + '[plugins.audit]\nsource = "../audit"\nversion = "1.0.0"\n'),
            PROJECT,
            "lock.toml:9: plugins.audit: is locked, but fastro.toml's [plugins] doesn't list it.\n"
            "  fastro writes both together: add audit back to fastro.toml's [plugins], or drop [plugins.audit] here.",
        ),
        (
            _text(),
            PROJECT,
            "fastro.toml:8: plugins.stripe: is in fastro.toml's [plugins], but the lock has no [plugins.stripe].\n"
            "  fastro writes both together: drop stripe from [plugins], or lock the version and commit it was built with.",
        ),
    ],
)
def test_a_lock_that_disagrees_with_fastro_toml_is_refused(lock: str, fastro: str, problem: str) -> None:
    refused = _against(lock, fastro)

    assert isinstance(refused, CrossReferenceError)
    assert [str(found) for found in refused.problems] == [problem]


def test_the_lock_is_checked_against_fastro_toml_only_once_its_keys_read_cleanly() -> None:
    lock = _text(tables=STRIPE).replace('name = "fastro"', 'name = "fastroai"').replace("schema = 1", "schema = 0")

    refused = _against(lock)

    assert not isinstance(refused, CrossReferenceError)
    assert [str(found) for found in refused.problems] == ["lock.toml:2: lock.schema: should be 1, not 0."]
