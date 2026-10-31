"""Rendering the wiring (spec 2.6 to 2.8): first, the order contributions take in a slot (2.7)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine import blueprint, project
from fastro.engine.blueprint import Blueprint, Contributions, Info
from fastro.engine.render import feature_indexes, import_block, ordered, ref_import, render_python
from fastro.engine.resolve import select, taken
from fastro.engine.spec import Ref, RepoPath

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini" / "blueprint.toml"

ORDERS = """\
[blueprint]
name = "orders"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[points.routers]
cardinality = "many"

[[wiring]]
path = "backend/src/wiring.py"
doc = "Wiring."

[[wiring.slots]]
kind = "tuple"
name = "ROUTERS"
point = "routers"
annotation = "tuple[object, ...]"

[core.contributes]
routers = ["src.core:health"]

[features.a]
summary = "A"

[features.a.contributes]
routers = ["src.a:first", { ref = "src.a:skipped", if = "c" }, "src.a:second"]

[[features.a.when]]
if = "b"
contributes = { routers = ["src.a:with_b"] }

[features.b]
summary = "B"

[features.b.contributes]
routers = [{ ref = "src.b:early", order = 50 }, "src.b:late"]

[features.c]
summary = "C"
"""


def _catalog(text: str = ORDERS) -> Blueprint:
    return blueprint.load("blueprint.toml", text, Version("0.1.0"))


def _routers(catalog: Blueprint, features: tuple[str, ...], app: str = "") -> list[str]:
    contributes: Contributions = {}
    if app:
        contributes = project.load(
            "fastro.toml",
            f'blueprint = {{ name = "orders", version = "0.1.0" }}\nfeatures = []\n[project]\nname = "p"\n{app}',
        ).app.contributes
    found = ordered(catalog, taken(catalog, select(catalog, features), contributes))
    return [str(item.contribution.target) for item in found["routers"]]


def test_mini_mounts_billing_before_accounts_by_its_explicit_order() -> None:
    mini = blueprint.load("blueprint.toml", MINI.read_text(), Version("0.1.0"))
    found = ordered(mini, taken(mini, select(mini, mini.presets["full"])))

    assert [item.owner for item in found["routers"]] == ["feature:billing", "feature:accounts"]


def test_core_comes_first_then_features_in_catalog_order_then_when_contributions_after_their_own() -> None:
    assert _routers(_catalog(), ("a", "b")) == [
        "src.core:health",
        "src.b:early",
        "src.a:first",
        "src.a:second",
        "src.a:with_b",
        "src.b:late",
    ]


def test_app_comes_last() -> None:
    routers = _routers(_catalog(), ("a",), app='[app]\ncontributes = { routers = ["src.app:mine"] }\n')

    assert routers == ["src.core:health", "src.a:first", "src.a:second", "src.app:mine"]


def test_an_explicit_order_below_100_goes_before_every_default() -> None:
    routers = _routers(_catalog(), ("a",), app='[app]\ncontributes = { routers = [{ ref = "src.app:first", order = 1 }] }\n')

    assert routers[:2] == ["src.core:health", "src.app:first"]


def test_a_position_counts_the_features_whole_list_whatever_is_selected() -> None:
    catalog = _catalog(ORDERS.replace('{ ref = "src.b:early", order = 50 }', '{ ref = "src.b:early", order = 101 }'))

    assert _routers(catalog, ("a", "b")) == [
        "src.core:health",
        "src.a:first",
        "src.b:early",
        "src.a:second",
        "src.a:with_b",
        "src.b:late",
    ]


def test_equal_orders_are_broken_by_feature_index_then_position() -> None:
    catalog = _catalog(
        ORDERS.replace('{ ref = "src.b:early", order = 50 }', '{ ref = "src.b:early", order = 7 }').replace(
            '"src.a:second"]', '{ ref = "src.a:second", order = 7 }]'
        )
    )

    assert _routers(catalog, ("a", "b"))[:3] == ["src.core:health", "src.a:second", "src.b:early"]


def test_a_when_that_fails_still_counts_toward_the_positions_after_it() -> None:
    catalog = _catalog(
        ORDERS.replace(
            '[[features.a.when]]\nif = "b"\ncontributes = { routers = ["src.a:with_b"] }\n',
            '[[features.a.when]]\nif = "c"\ncontributes = { routers = ["src.a:with_c"] }\n'
            '[[features.a.when]]\nif = "b"\ncontributes = { routers = ["src.a:with_b"] }\n',
        ).replace('{ ref = "src.b:early", order = 50 }', '{ ref = "src.b:early", order = 103 }')
    )

    assert _routers(catalog, ("a", "b")) == [
        "src.core:health",
        "src.a:first",
        "src.a:second",
        "src.b:early",
        "src.a:with_b",
        "src.b:late",
    ]


def test_the_feature_index_is_minus_one_for_core_then_each_feature_then_app() -> None:
    assert feature_indexes(_catalog()) == {"core": -1, "feature:a": 0, "feature:b": 1, "feature:c": 2, "app": 3}


@pytest.mark.parametrize("features", [("a", "b"), ("b", "a")])
def test_the_order_doesnt_depend_on_the_order_features_were_asked_for(features: tuple[str, ...]) -> None:
    assert _routers(_catalog(), features)[0:2] == ["src.core:health", "src.b:early"]


INFO = Info("mini", Version("0.1.0"), 1, SpecifierSet(">=0.1"))


@pytest.mark.parametrize(
    ("ref", "file", "line"),
    [
        (
            "src.modules.tier.routes:router as tiers_router",
            "backend/src/wiring/app.py",
            "from ..modules.tier.routes import router as tiers_router",
        ),
        ("src.modules.tier.routes:router", "backend/src/app.py", "from .modules.tier.routes import router"),
        ("src.wiring.mount:Mount", "backend/src/wiring/app.py", "from .mount import Mount"),
        ("src.wiring.sub.thing:Thing", "backend/src/wiring/app.py", "from .sub.thing import Thing"),
        ("src:app", "backend/src/wiring/app.py", "from .. import app"),
        ("src.wiring:router", "backend/src/wiring/app.py", "from . import router"),
        ("fastapi:APIRouter", "backend/src/wiring/app.py", "from fastapi import APIRouter"),
        ("srcs.thing:x", "backend/src/wiring/app.py", "from srcs.thing import x"),
        ("src.modules.tier.routes:router", "backend/tests/wiring.py", "from src.modules.tier.routes import router"),
        ("src.modules.tier.routes:router", "backend/scripts/seed.py", "from src.modules.tier.routes import router"),
    ],
)
def test_a_ref_under_import_root_is_imported_relative_to_its_files_package(ref: str, file: str, line: str) -> None:
    assert ref_import(Ref.parse(ref), RepoPath(file), INFO) == line


def test_a_ref_honours_a_python_root_and_import_root_of_its_own() -> None:
    info = Info("mini", Version("0.1.0"), 1, SpecifierSet(">=0.1"), python_root=RepoPath("server"), import_root="app")

    assert (
        ref_import(Ref.parse("app.core.db:engine"), RepoPath("server/app/wiring/db.py"), info) == "from ..core.db import engine"
    )


def _block(*lines: str, line_length: int = 128) -> str:
    return import_block(lines, "src", line_length)


def test_2_8s_role_and_tier_examples_order_statements_from_one_module_by_their_first_name() -> None:
    block = _block(
        "from ..modules.role.routes import router as roles_router",
        "from ..modules.role.routes import user_roles_router",
        "from ..modules.role.routes import permissions_router",
        "from ..modules.tier.routes import user_tier_router",
        "from ..modules.tier.routes import router as tiers_router",
    )

    assert block == (
        "from ..modules.role.routes import permissions_router, user_roles_router\n"
        "from ..modules.role.routes import router as roles_router\n"
        "from ..modules.tier.routes import router as tiers_router\n"
        "from ..modules.tier.routes import user_tier_router\n"
    )


@pytest.mark.parametrize(
    ("lines", "block"),
    [
        (("from os import APIRouter, Ab",), "from os import Ab, APIRouter\n"),
        (("from os import b, B, a",), "from os import B, a, b\n"),
        (("from m import ab, aB",), "from m import aB, ab\n"),
        (("from m import aB, ab",), "from m import aB, ab\n"),
        (("import src as M2", "import src"), "import src\nimport src as M2\n"),
    ],
)
def test_names_compare_without_regard_to_case_first_and_a_plain_import_comes_before_its_alias(
    lines: tuple[str, ...], block: str
) -> None:
    assert _block(*lines) == block


def test_names_from_one_module_merge_as_constants_then_classes_then_functions() -> None:
    assert _block(
        "from fastapi import FastAPI", "from fastapi import Depends", "from fastapi import APIRouter, status, HTTP_OK"
    ) == ("from fastapi import HTTP_OK, APIRouter, Depends, FastAPI, status\n")


def test_the_sections_come_in_order_with_plain_imports_first_in_each() -> None:
    block = _block(
        "from ..mount import Mount",
        "from src.core import settings",
        "import logging",
        "from collections.abc import Callable",
        "from fastapi import APIRouter",
        "import httpx",
        "from .local import thing",
        "from __future__ import annotations",
        "from scripts.seed import seed",
        "import tests.wiring",
    )

    assert block == (
        "from __future__ import annotations\n"
        "\n"
        "import logging\n"
        "from collections.abc import Callable\n"
        "\n"
        "import httpx\n"
        "from fastapi import APIRouter\n"
        "\n"
        "import tests.wiring\n"
        "from scripts.seed import seed\n"
        "from src.core import settings\n"
        "\n"
        "from ..mount import Mount\n"
        "from .local import thing\n"
    )


def test_a_repeated_import_is_written_once_and_plain_imports_split() -> None:
    assert _block("import os, sys", "import os", "from os import path", "from os import path") == (
        "import os\nimport sys\nfrom os import path\n"
    )


def test_modules_sort_without_regard_to_case() -> None:
    assert _block("from Zeta import z", "from alpha import a") == "from alpha import a\nfrom Zeta import z\n"


def test_a_statement_wraps_only_past_the_line_length() -> None:
    line = "from ..modules.accounts.routes import accounts_router, login_router"

    assert _block(line, line_length=len(line)) == line + "\n"
    assert _block(line, line_length=len(line) - 1) == (
        "from ..modules.accounts.routes import (\n    accounts_router,\n    login_router,\n)\n"
    )


def test_an_aliased_import_past_the_line_length_wraps_alone() -> None:
    assert _block("from ..modules.accounts.routes import router as accounts_router", line_length=40) == (
        "from ..modules.accounts.routes import (\n    router as accounts_router,\n)\n"
    )


def test_no_imports_make_no_block() -> None:
    assert _block() == ""


def test_names_and_modules_with_numbers_sort_as_numbers_the_way_ruff_does() -> None:
    assert _block("from api.v10 import r10, r2, r1", "from api.v2 import x") == (
        "from api.v2 import x\nfrom api.v10 import r1, r2, r10\n"
    )


@pytest.mark.parametrize(
    ("lines", "block"),
    [
        (
            (
                "from . import router",
                "from .. import app",
                "from ..modules.x import y",
                "from .local import thing",
                "from .a import b",
            ),
            "from .. import app\nfrom ..modules.x import y\nfrom . import router\nfrom .a import b\nfrom .local import thing\n",
        ),
        (("from beta import Base, base, BASE, B, b",), "from beta import BASE, B, Base, b, base\n"),
        (
            ("import numpy as np", "import os", "import datetime as dt", "from os import path as p", "from os import sep"),
            "import datetime as dt\nimport os\nfrom os import path as p\nfrom os import sep\n\nimport numpy as np\n",
        ),
    ],
)
def test_these_blocks_come_out_as_written(lines: tuple[str, ...], block: str) -> None:
    assert _block(*lines) == block


def test_a_line_that_isnt_an_import_is_refused() -> None:
    with pytest.raises(ValueError, match="'x = 1' isn't an import statement"):
        _block("x = 1")


MODULES = ["os", "typing", "collections.abc", "fastapi", "pydantic", "FastAPI_x", "sqlalchemy.orm", "src", "src.core"]
MODULES += ["src.Core", "src.modules.r2", "src.modules.r10", "scripts.seed", "tests.wiring", "zlib", "Abc"]
RELATIVE = ["..", ".", "..modules.tier.routes", "..modules.Tier", ".sibling"]
PLAIN = ["os", "src", "fastapi"]
NAMES = ["APIRouter", "AbcClass", "a", "B", "CONST", "C2", "C10", "r2", "r10", "router", "Router", "_private", "_Private"]
NAMES += ["user_tier_router", "x", "X", "ab", "aB", "Ab"]
RUFF_CONFIG = """\
[tool.ruff]
line-length = 128

[tool.ruff.lint]
select = ["I"]

[tool.ruff.lint.isort]
known-first-party = ["src"]
"""


@st.composite
def import_lines(draw: st.DrawFn) -> list[str]:
    lines = []
    for _ in range(draw(st.integers(1, 8))):
        if draw(st.booleans()) and draw(st.booleans()):
            alias = draw(st.sampled_from(["", " as m", " as M2", " as alias"]))
            lines.append(f"import {draw(st.sampled_from(PLAIN))}{alias}")
            continue
        module = draw(st.sampled_from(RELATIVE + MODULES))
        names = draw(st.lists(st.sampled_from(NAMES), min_size=1, max_size=4, unique=True))
        parts = [name + (f" as {name}_alias" if draw(st.booleans()) and draw(st.booleans()) else "") for name in names]
        lines.append(f"from {module} import {', '.join(parts)}")
    return lines


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("isort") / "backend"
    for package in ("src", "src/wiring", "scripts", "tests"):
        (root / package).mkdir(parents=True)
        (root / package / "__init__.py").write_text("")
    for module in ("scripts/seed.py", "tests/wiring.py"):
        (root / module).write_text("")
    (root / "pyproject.toml").write_text(RUFF_CONFIG)
    return root


@settings(database=None, deadline=None, derandomize=True, max_examples=100)
@given(lines=import_lines())
def test_the_pinned_ruff_accepts_every_import_block_as_it_is(backend: Path, lines: list[str]) -> None:
    file = backend / "src" / "wiring" / "x.py"
    file.write_text('"""Imports."""\n\n' + _block(*lines))

    run = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--diff", str(file)], cwd=backend, capture_output=True, text=True
    )

    assert run.returncode == 0, f"{lines}\n{run.stdout}{run.stderr}"


def test_an_import_block_is_the_same_whatever_the_hash_seed() -> None:
    lines = ["from m import ab, aB, Ab, AB", "import os", "import os as o", "from m import x as y", "from m import z as w"]
    script = f"from fastro.engine.render import import_block; print(import_block({lines!r}, 'src', 128), end='')"
    outputs = {
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("0", "1", "2", "3")
    }

    assert outputs == {_block(*lines)}


ASSIGNMENTS = """\
[blueprint]
name = "assign"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[wiring_defaults]
header = \"\"\"
Generated by fastro.
\"\"\"

[points.routers]
cardinality = "many"

[points.jobs]
cardinality = "many"

[points.sender]
cardinality = "one"

[points.labels]
cardinality = "many"

[[wiring]]
path = "backend/src/wiring/app.py"
doc = "The app."
imports = ["from collections.abc import Callable"]

[[wiring.slots]]
kind = "tuple"
name = "ROUTERS"
point = "routers"
annotation = "tuple[Mount, ...]"
item = 'Mount({name}, "{prefix}", throttled={throttled})'
defaults = { prefix = "", throttled = true }
item_imports = ["from ..mount import Mount"]

[[wiring.slots]]
kind = "list"
name = "JOBS"
point = "jobs"
annotation = "list[object]"

[[wiring.slots]]
kind = "value"
name = "SENDER"
point = "sender"
annotation = "Callable[[str], None]"
fallback = "send_nowhere"
fallback_imports = ["from ..email import send_nowhere"]

[[wiring.slots]]
kind = "list"
name = "LABELS"
point = "labels"
annotation = "list[str]"
literal = true

[features.a]
summary = "A"

[features.a.contributes]
routers = [{ ref = "src.a.routes:router as a_router", prefix = "/a" }]
jobs = [{ ref = "src.a.jobs:Job", call = true }]
sender = ["src.a.email:send"]
labels = ["A", { value = "Alpha" }]

[features.b]
summary = "B"

[features.b.contributes]
routers = [{ ref = "src.b.routes:router", throttled = false }]
"""


def _python(text: str, features: tuple[str, ...]) -> str:
    catalog = _catalog(text)
    found = ordered(catalog, taken(catalog, select(catalog, features)))
    return render_python(catalog, catalog.wiring[0], found)


def test_with_nothing_contributed_each_assignment_is_empty_and_the_value_falls_back() -> None:
    assert _python(ASSIGNMENTS, ()) == (
        '"""The app.\n\nGenerated by fastro.\n"""\n'
        "\n"
        "from collections.abc import Callable\n"
        "\n"
        "from ..email import send_nowhere\n"
        "\n"
        "ROUTERS: tuple[Mount, ...] = ()\n"
        "JOBS: list[object] = []\n"
        "SENDER: Callable[[str], None] = send_nowhere\n"
        "LABELS: list[str] = []\n"
    )


def test_one_contribution_puts_a_tuple_on_one_line_and_explodes_a_list() -> None:
    assert _python(ASSIGNMENTS, ("a",)) == (
        '"""The app.\n\nGenerated by fastro.\n"""\n'
        "\n"
        "from collections.abc import Callable\n"
        "\n"
        "from ..a.email import send\n"
        "from ..a.jobs import Job\n"
        "from ..a.routes import router as a_router\n"
        "from ..mount import Mount\n"
        "\n"
        'ROUTERS: tuple[Mount, ...] = (Mount(a_router, "/a", throttled=True),)\n'
        "JOBS: list[object] = [\n"
        "    Job(),\n"
        "]\n"
        "SENDER: Callable[[str], None] = send\n"
        "LABELS: list[str] = [\n"
        '    "A",\n'
        '    "Alpha",\n'
        "]\n"
    )


def test_two_contributions_explode_a_tuple_with_defaults_filling_what_one_leaves_out() -> None:
    rendered = _python(ASSIGNMENTS, ("a", "b"))

    assert (
        "ROUTERS: tuple[Mount, ...] = (\n"
        '    Mount(a_router, "/a", throttled=True),\n'
        '    Mount(router, "", throttled=False),\n'
        ")\n"
    ) in rendered
    assert "from ..b.routes import router\n" in rendered


def test_a_value_slot_renders_its_contribution_by_its_alias() -> None:
    text = ASSIGNMENTS.replace('sender = ["src.a.email:send"]', 'sender = ["src.a.email:send as a_send"]')

    rendered = _python(text, ("a",))

    assert "SENDER: Callable[[str], None] = a_send\n" in rendered
    assert "from ..a.email import send as a_send\n" in rendered


def test_a_file_without_imports_puts_its_first_slot_one_blank_line_after_the_docstring() -> None:
    text = ASSIGNMENTS.replace('imports = ["from collections.abc import Callable"]\n', "").replace(
        'annotation = "Callable[[str], None]"', 'annotation = "object"'
    )
    text = text.replace(
        'fallback = "send_nowhere"\nfallback_imports = ["from ..email import send_nowhere"]', 'fallback = "None"'
    )

    assert _python(text, ()).startswith('"""The app.\n\nGenerated by fastro.\n"""\n\nROUTERS: tuple[Mount, ...] = ()\n')


def test_without_a_header_the_docstring_is_the_doc_alone() -> None:
    text = ASSIGNMENTS.replace('[wiring_defaults]\nheader = """\nGenerated by fastro.\n"""\n', "")

    assert _python(text, ()).startswith('"""The app."""\n\nfrom collections.abc import Callable\n')


def test_a_value_slot_with_no_contribution_and_no_fallback_is_an_error() -> None:
    text = ASSIGNMENTS.replace('fallback = "send_nowhere"\nfallback_imports = ["from ..email import send_nowhere"]\n', "")

    with pytest.raises(ValueError, match="SENDER has no contribution and no fallback"):
        _python(text, ())


@pytest.mark.parametrize("name", ["health.py", "tasks.py"])
def test_mini_s_assignment_only_wiring_renders_as_committed(name: str) -> None:
    mini = blueprint.load("blueprint.toml", MINI.read_text(), Version("0.1.0"))
    file = next(file for file in mini.wiring if file.path.text == f"backend/src/wiring/{name}")
    found = ordered(mini, taken(mini, select(mini, mini.presets["full"])))

    assert render_python(mini, file, found) == (MINI.parent / file.path.text).read_text()
