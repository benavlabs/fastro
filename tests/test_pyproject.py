"""``{python_root}/pyproject.toml`` rewritten for a selection: the runtime dependencies gathered and merged, each dev
table pruned, everything else kept (spec 6.3, A4 item 5)."""

from pathlib import Path

import pytest
import tomlkit
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.resolve import select
from fastro.pyproject import PyprojectError, rewrite

MINI = (Path(__file__).parent / "fixtures" / "blueprints" / "mini" / "blueprint.toml").read_text()
PYPROJECT = """\
# the blueprint's own
[project]
name = "mini-backend"
version = "0.1.0"
dependencies = [
    "old>=1",  # replaced
]

[project.optional-dependencies]
dev = [
    # the test tools
    "pytest>=8",  # always
    "fakeredis[lua]>=2",  # for the cache
    "Paddle_Stubs",
    "ruff==0.15",
]
docs = ["mkdocs"]

[dependency-groups]
dev = ["fakeredis", { include-group = "lint" }, "not a requirement ~", "freezegun"]
lint = ["ruff"]

[tool.ruff]
line-length = 120  # wide
"""


def _catalog(
    core: str = "",
    cache: str = "",
    redis: str = "",
    backend: str = "",
    billing: str = "",
    paddle: str = "",
    provider: str = "",
) -> Blueprint:
    text = MINI.replace("[presets]\n", f"[blueprint.dependencies]\n{core}\n\n[presets]\n")
    text = text.replace('summary = "A cache for slow reads"\n', f'summary = "A cache for slow reads"\n{cache}\n')
    text = text.replace('services = ["redis"]\n', f'services = ["redis"]\n{redis}\n')
    text = text.replace('default = "memory"\n', f'default = "memory"\n{backend}\n')
    text = text.replace('summary = "Payments"\n', f'summary = "Payments"\n{billing}\n')
    text = text.replace(
        'paths = ["backend/src/modules/billing/paddle.py"]\n', f'paths = ["backend/src/modules/billing/paddle.py"]\n{paddle}\n'
    )
    text = text.replace('default = "stripe"\n', f'default = "stripe"\n{provider}\n')
    return blueprint.load("blueprint.toml", text, Version("0.1.0"))


def _rewrite(
    catalog: Blueprint,
    features: tuple[str, ...] = (),
    options: dict[tuple[str, str], str] | None = None,
    text: str = PYPROJECT,
) -> str:
    return rewrite(text, catalog, select(catalog, features, options=options))


def _runtime(
    catalog: Blueprint, features: tuple[str, ...] = (), options: dict[tuple[str, str], str] | None = None
) -> list[str]:
    return list(tomlkit.parse(_rewrite(catalog, features, options))["project"]["dependencies"])


def _dev(text: str) -> tuple[list[object], list[object]]:
    document = tomlkit.parse(text)
    return (
        list(document["project"]["optional-dependencies"]["dev"]),
        list(document["dependency-groups"]["dev"]),
    )


def test_core_alone_gives_the_core_runtime_dependencies_sorted_by_name_as_written() -> None:
    catalog = _catalog(core='runtime = ["pydantic >=2", "FastAPI[standard]>=0.115", "alembic"]')

    assert _runtime(catalog) == ["alembic", "FastAPI[standard]>=0.115", "pydantic >=2"]


def test_a_selected_feature_adds_its_dependencies_and_an_unselected_one_doesnt() -> None:
    catalog = _catalog(
        core='runtime = ["fastapi"]', cache='dependencies = ["cachetools>=5"]', billing='dependencies = ["stripe"]'
    )

    assert _runtime(catalog, ("cache",)) == ["cachetools>=5", "fastapi"]


def test_a_when_counts_only_while_its_if_holds() -> None:
    catalog = _catalog(redis='dependencies = ["redis>=5"]')

    assert _runtime(catalog, ("cache",), {("cache", "backend"): "redis"}) == ["redis>=5"]
    assert _runtime(catalog, ("cache",)) == []


def test_an_option_adds_the_dependencies_of_its_chosen_value_only() -> None:
    catalog = _catalog(backend='dependencies = { redis = ["redis>=5"], memory = ["cachetools"] }')

    assert _runtime(catalog, ("cache",), {("cache", "backend"): "redis"}) == ["redis>=5"]
    assert _runtime(catalog, ("cache",)) == ["cachetools"]
    assert _runtime(catalog) == []


def test_entries_for_one_package_become_one_with_specifiers_intersected_and_extras_joined() -> None:
    catalog = _catalog(
        core='runtime = ["Redis[zstd]>=4"]',
        cache='dependencies = ["redis[ocsp,jwt]<6"]',
        backend='dependencies = { redis = ["redis[hiredis]!=5.0.1"] }',
    )

    assert _runtime(catalog, ("cache",), {("cache", "backend"): "redis"}) == ["Redis[hiredis,jwt,ocsp,zstd]!=5.0.1,<6,>=4"]


def test_names_are_compared_normalized() -> None:
    catalog = _catalog(core='runtime = ["Foo_Bar>=1"]', cache='dependencies = ["foo.bar<2"]')

    assert _runtime(catalog, ("cache",)) == ["Foo_Bar<2,>=1"]


def test_entries_with_the_same_marker_written_differently_are_merged_keeping_it() -> None:
    catalog = _catalog(
        core="""runtime = ["uvloop>=0.19; sys_platform != 'win32'"]""",
        cache="""dependencies = ['uvloop<1 ;sys_platform!="win32"']""",
    )

    assert _runtime(catalog, ("cache",)) == ['uvloop<1,>=0.19; sys_platform != "win32"']


@pytest.mark.parametrize(
    "cache",
    ["""dependencies = ["uvloop; sys_platform == 'linux'"]""", 'dependencies = ["uvloop"]'],
    ids=["different", "one-without"],
)
def test_entries_with_different_environment_markers_are_refused(cache: str) -> None:
    catalog = _catalog(core="""runtime = ["uvloop; sys_platform != 'win32'"]""", cache=cache)

    with pytest.raises(PyprojectError, match="^uvloop has entries with different environment markers: "):
        _rewrite(catalog, ("cache",))


def test_the_same_direct_reference_twice_keeps_the_first_as_written() -> None:
    catalog = _catalog(
        core='runtime = ["pkg[b,a] @ https://example.com/pkg-1.0.tar.gz"]',
        cache='dependencies = ["PKG[a,b]@https://example.com/pkg-1.0.tar.gz"]',
    )

    assert _runtime(catalog, ("cache",)) == ["pkg[b,a] @ https://example.com/pkg-1.0.tar.gz"]


@pytest.mark.parametrize(
    "cache",
    [
        'dependencies = ["pkg @ https://example.com/pkg-2.0.tar.gz"]',
        'dependencies = ["pkg>=1"]',
        'dependencies = ["pkg[x] @ https://example.com/pkg-1.0.tar.gz"]',
        """dependencies = ["pkg @ https://example.com/pkg-1.0.tar.gz ; os_name == 'nt'"]""",
    ],
    ids=["another-url", "a-specifier", "other-extras", "a-marker"],
)
def test_a_direct_reference_that_isnt_the_same_as_another_entry_for_its_package_is_refused(cache: str) -> None:
    catalog = _catalog(core='runtime = ["pkg @ https://example.com/pkg-1.0.tar.gz"]', cache=cache)

    with pytest.raises(PyprojectError, match="^pkg has a direct reference, and its entries aren't the same requirement: "):
        _rewrite(catalog, ("cache",))


def test_dependencies_are_written_when_the_file_has_none() -> None:
    text = '[project]\nname = "x"\nversion = "1"\n'

    assert _rewrite(_catalog(), text=text) == '[project]\nname = "x"\nversion = "1"\ndependencies = []\n'


def test_each_dev_table_loses_what_only_unselected_features_declare_and_keeps_the_rest_as_written() -> None:
    catalog = _catalog(cache='dev_dependencies = ["fakeredis"]', billing='dev_dependencies = ["paddle-stubs", "freezegun"]')

    rewritten = _rewrite(catalog, ("cache",))

    assert _dev(rewritten) == (
        ["pytest>=8", "fakeredis[lua]>=2", "ruff==0.15"],
        ["fakeredis", {"include-group": "lint"}, "not a requirement ~"],
    )
    assert '    # the test tools\n    "pytest>=8",  # always\n    "fakeredis[lua]>=2",  # for the cache\n' in rewritten


def test_an_entry_a_selected_feature_also_declares_stays() -> None:
    catalog = _catalog(cache='dev_dependencies = ["freezegun"]', billing='dev_dependencies = ["freezegun"]')

    assert "freezegun" in _dev(_rewrite(catalog, ("cache",)))[1]


def test_a_when_s_dev_dependencies_count_only_while_its_feature_is_selected_and_its_if_holds() -> None:
    catalog = _catalog(redis='dev_dependencies = ["fakeredis"]')

    assert "fakeredis" in _dev(_rewrite(catalog, ("cache",), {("cache", "backend"): "redis"}))[1]
    assert "fakeredis" not in _dev(_rewrite(catalog, ("cache",)))[1]
    assert "fakeredis" not in _dev(_rewrite(catalog))[1]


def test_a_when_of_an_unselected_feature_doesnt_keep_an_entry_its_if_would_hold_for() -> None:
    catalog = _catalog(paddle='dev_dependencies = ["paddle-stubs"]')

    assert "Paddle_Stubs" not in _dev(_rewrite(catalog, (), {("billing", "provider"): "paddle"}))[0]
    assert "Paddle_Stubs" in _dev(_rewrite(catalog, ("billing",), {("billing", "provider"): "paddle"}))[0]


def test_core_dev_dependencies_stay_whether_blueprint_dependencies_dev_lists_them_or_not() -> None:
    catalog = _catalog(core='dev = ["pytest"]')

    assert _dev(_rewrite(catalog)) == (
        ["pytest>=8", "fakeredis[lua]>=2", "Paddle_Stubs", "ruff==0.15"],
        ["fakeredis", {"include-group": "lint"}, "not a requirement ~", "freezegun"],
    )


def test_everything_else_is_kept_name_and_version_included() -> None:
    catalog = _catalog(core='runtime = ["fastapi"]')

    assert _rewrite(catalog) == PYPROJECT.replace('    "old>=1",  # replaced\n', '    "fastapi",\n')


def test_a_byte_order_mark_is_kept() -> None:
    assert _rewrite(_catalog(), text="﻿" + PYPROJECT).startswith("﻿# the blueprint's own\n")


def test_a_file_without_dev_tables_gets_none() -> None:
    text = '[project]\nname = "x"\nversion = "1"\ndependencies = []\n'

    assert _rewrite(_catalog(cache='dev_dependencies = ["fakeredis"]'), text=text) == text
