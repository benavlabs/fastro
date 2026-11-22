"""Rendering the wiring (spec 2.6 to 2.8): first, the order contributions take in a slot (2.7)."""

import json
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
from fastro.engine.plan import Plan, plan
from fastro.engine.render import (
    RenderError,
    Shadows,
    feature_indexes,
    import_block,
    ordered,
    ref_import,
    render,
    render_data,
    render_python,
    shadows,
)
from fastro.engine.resolve import NoContribution, TooManyContributions, select, taken
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
    with pytest.raises(RenderError, match="'x = 1' isn't an import statement"):
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
annotation = "tuple[object, ...]"
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
        "ROUTERS: tuple[object, ...] = ()\n"
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
        'ROUTERS: tuple[object, ...] = (Mount(a_router, "/a", throttled=True),)\n'
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
        "ROUTERS: tuple[object, ...] = (\n"
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

    assert _python(text, ()).startswith('"""The app.\n\nGenerated by fastro.\n"""\n\nROUTERS: tuple[object, ...] = ()\n')


def test_without_a_header_the_docstring_is_the_doc_alone() -> None:
    text = ASSIGNMENTS.replace('[wiring_defaults]\nheader = """\nGenerated by fastro.\n"""\n', "")

    assert _python(text, ()).startswith('"""The app."""\n\nfrom collections.abc import Callable\n')


def test_a_value_slot_with_no_contribution_and_no_fallback_is_an_error() -> None:
    text = ASSIGNMENTS.replace('fallback = "send_nowhere"\nfallback_imports = ["from ..email import send_nowhere"]\n', "")

    with pytest.raises(RenderError, match="SENDER has no contribution and no fallback"):
        _python(text, ())


@pytest.mark.parametrize("name", ["app.py", "admin.py", "health.py", "tasks.py"])
def test_mini_s_python_wiring_renders_as_committed(name: str) -> None:
    mini = blueprint.load("blueprint.toml", MINI.read_text(), Version("0.1.0"))
    file = next(file for file in mini.wiring if file.path.text == f"backend/src/wiring/{name}")
    found = ordered(mini, taken(mini, select(mini, mini.presets["full"])))

    assert render_python(mini, file, found) == (MINI.parent / file.path.text).read_text()


CLASSES = '''\
[blueprint]
name = "classes"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[wiring_defaults]
header = """
Generated.
"""

[points.settings]
cardinality = "many"

[points.site]
cardinality = "one"

[[wiring]]
path = "backend/src/wiring/app.py"
doc = "The app."

[[wiring.slots]]
kind = "class"
name = "Settings"
point = "settings"
fallback = "pydantic:BaseModel"
append = ["src.core.config:CoreSettings"]
doc = "Every feature's settings."

[[wiring.slots]]
kind = "raw"
code = "settings = Settings()"

[[wiring.slots]]
kind = "reexport"
name = "SITE"
point = "site"
fallback = "src.admin_default:site"

[features.a]
summary = "A"

[features.a.contributes]
settings = ["src.a.settings:ASettings"]
site = ["src.a.site:site as admin_site"]

[features.b]
summary = "B"

[features.b.contributes]
settings = ["src.b.settings:BSettings"]
'''
FALLBACK_CLASS = 'fallback = "pydantic:BaseModel"\nappend = ["src.core.config:CoreSettings"]\n'
REEXPORT_SLOT = '[[wiring.slots]]\nkind = "reexport"\nname = "SITE"\npoint = "site"\nfallback = "src.admin_default:site"\n'
RAW_SLOT = '[[wiring.slots]]\nkind = "raw"\ncode = "settings = Settings()"\n'


def _unexported(text: str) -> str:
    """``text`` without its reexport slot, the point it collects and the contribution to it."""
    for piece in (REEXPORT_SLOT, '[points.site]\ncardinality = "one"\n\n', 'site = ["src.a.site:site as admin_site"]\n'):
        assert piece in text
        text = text.replace(piece, "")
    return text


def test_with_nothing_contributed_a_class_takes_its_fallback_and_append_and_a_reexport_its_fallback() -> None:
    assert _python(CLASSES, ()) == (
        '"""The app.\n\nGenerated.\n"""\n'
        "\n"
        "from pydantic import BaseModel\n"
        "\n"
        "from ..admin_default import site as SITE\n"
        "from ..core.config import CoreSettings\n"
        "\n"
        "\n"
        "class Settings(BaseModel, CoreSettings):\n"
        '    """Every feature\'s settings."""\n'
        "\n"
        "\n"
        "settings = Settings()\n"
        '__all__ = ["SITE"]\n'
    )


def test_contributions_replace_a_class_s_fallback_and_a_reexport_s_by_their_order() -> None:
    assert _python(CLASSES, ("a", "b")) == (
        '"""The app.\n\nGenerated.\n"""\n'
        "\n"
        "from ..a.settings import ASettings\n"
        "from ..a.site import site as SITE\n"
        "from ..b.settings import BSettings\n"
        "from ..core.config import CoreSettings\n"
        "\n"
        "\n"
        "class Settings(ASettings, BSettings, CoreSettings):\n"
        '    """Every feature\'s settings."""\n'
        "\n"
        "\n"
        "settings = Settings()\n"
        '__all__ = ["SITE"]\n'
    )


def test_a_reexport_whose_attr_is_its_name_imports_it_without_an_alias() -> None:
    rendered = _python(CLASSES.replace('name = "SITE"', 'name = "site"'), ())

    assert "from ..admin_default import site\n" in rendered
    assert rendered.endswith('__all__ = ["site"]\n')


@pytest.mark.parametrize(("extra", "exploded"), [(0, False), (-1, True)])
def test_a_class_s_bases_explode_only_past_the_line_length(extra: int, exploded: bool) -> None:
    line = "class Settings(ASettings, BSettings, CoreSettings):"
    text = CLASSES.replace("[points.settings]", f"line_length = {len(line) + extra}\n\n[points.settings]")

    rendered = _python(text, ("a", "b"))

    split = "class Settings(\n    ASettings,\n    BSettings,\n    CoreSettings,\n):\n"
    assert (split in rendered, line + "\n" in rendered) == (exploded, not exploded)


@pytest.mark.parametrize(
    ("bases", "line"),
    [
        ('fallback = "pydantic:BaseModel"\n', "class Settings(BaseModel):\n"),
        ('append = ["src.core.config:CoreSettings"]\n', "class Settings(CoreSettings):\n"),
        ("", "class Settings:\n"),
    ],
)
def test_a_class_with_nothing_contributed_has_its_fallback_its_append_or_no_bases(bases: str, line: str) -> None:
    assert line in _python(CLASSES.replace(FALLBACK_CLASS, bases), ())


def test_a_class_s_fallback_import_comes_only_with_the_fallback() -> None:
    assert "pydantic" not in _python(CLASSES, ("b",))


def test_a_class_doc_of_several_paragraphs_is_indented_with_its_quotes_on_their_own_line() -> None:
    text = CLASSES.replace('doc = "Every feature\'s settings."', 'doc = """\nFirst.\n\nSecond line.\nThird.\n"""')

    assert '    """First.\n\n    Second line.\n    Third.\n    """\n\n\nsettings' in _python(text, ())


def test_a_class_right_after_the_docstring_gets_two_blank_lines() -> None:
    text = _unexported(CLASSES.replace(FALLBACK_CLASS, ""))

    assert _python(text, ()) == (
        '"""The app.\n\nGenerated.\n"""\n\n\nclass Settings:\n    """Every feature\'s settings."""\n\n\nsettings = Settings()\n'
    )


def test_a_file_ending_in_a_class_ends_with_one_newline() -> None:
    text = _unexported(CLASSES.replace(RAW_SLOT, ""))

    assert _python(text, ()).endswith('class Settings(BaseModel, CoreSettings):\n    """Every feature\'s settings."""\n')


def test_raw_code_is_rendered_without_its_outer_newlines() -> None:
    text = CLASSES.replace('code = "settings = Settings()"', 'code = """\nsettings = Settings()\nready = True\n"""')

    assert _python(text, ()).endswith('"""\n\n\nsettings = Settings()\nready = True\n__all__ = ["SITE"]\n')


@pytest.mark.parametrize(
    "code",
    [
        "def make() -> int:\n    return 1",
        "async def make() -> int:\n    return 1",
        "class Made:\n    pass",
        "@staticmethod\ndef make() -> int:\n    return 1",
    ],
)
def test_raw_code_that_starts_with_a_def_or_a_class_is_spaced_like_a_class(code: str) -> None:
    raws = "".join(f'[[wiring.slots]]\nkind = "raw"\ncode = {json.dumps(piece)}\n\n' for piece in ("x = 1", code, "y = 2"))
    text = _unexported(CLASSES.replace(FALLBACK_CLASS, "").replace(RAW_SLOT, raws))

    assert f'settings."""\n\n\nx = 1\n\n\n{code}\n\n\ny = 2\n' in _python(text, ())


def test_raw_code_that_starts_with_an_assignment_sits_beside_the_next_one() -> None:
    raws = "".join(f'[[wiring.slots]]\nkind = "raw"\ncode = {json.dumps(piece)}\n\n' for piece in ("x = 1", "y = 2"))

    assert "\nx = 1\ny = 2\n" in _python(_unexported(CLASSES.replace(RAW_SLOT, raws)), ())


def test_a_reexport_with_no_contribution_and_no_fallback_is_an_error() -> None:
    text = CLASSES.replace('fallback = "src.admin_default:site"\n', "")

    with pytest.raises(RenderError, match="SITE has no contribution and no fallback"):
        _python(text, ())


LAYOUTS = {
    "fallback": (CLASSES, ()),
    "contributed": (CLASSES, ("a", "b")),
    "class first": (_unexported(CLASSES.replace(FALLBACK_CLASS, "")), ()),
    "class last": (_unexported(CLASSES.replace(RAW_SLOT, "")), ()),
    "raw def": (
        CLASSES.replace(
            RAW_SLOT,
            "".join(
                f'[[wiring.slots]]\nkind = "raw"\ncode = {json.dumps(piece)}\n\n'
                for piece in ("x = 1", "@staticmethod\ndef make() -> int:\n    return 1", "y = 2")
            ),
        ),
        (),
    ),
    "paragraphs": (CLASSES.replace('doc = "Every feature\'s settings."', 'doc = """\nFirst.\n\nSecond line.\n"""'), ()),
}


@pytest.mark.parametrize("name", list(LAYOUTS))
def test_the_pinned_ruff_formats_each_layout_as_it_is(backend: Path, name: str) -> None:
    text, features = LAYOUTS[name]
    file = backend / "src" / "wiring" / "layout.py"
    file.write_text(_python(text, features))

    run = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--no-cache", "--diff", str(file)], cwd=backend, capture_output=True, text=True
    )

    assert run.returncode == 0, run.stdout + run.stderr


def test_a_class_without_bases_stays_on_one_line_past_the_line_length() -> None:
    text = _unexported(
        CLASSES.replace(FALLBACK_CLASS, "").replace("[points.settings]", "line_length = 10\n\n[points.settings]")
    )

    assert "\nclass Settings:\n" in _python(text, ())


def test_raw_code_that_is_only_a_comment_sits_beside_the_next_slot() -> None:
    assert "\n# Kept.\nsettings = Settings()\n" in _python(
        CLASSES.replace(RAW_SLOT, RAW_SLOT.replace("settings = Settings()", "# Kept.") + RAW_SLOT), ()
    )


DATA = '''\
[blueprint]
name = "data"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[wiring_defaults]
header = """
Generated.
Don't edit.
"""

[points.menu]
cardinality = "many"

[points.flags]
cardinality = "many"

[core.contributes]
menu = [{ data = { label = "Home", href = "/" }, order = 1 }]

[[wiring]]
path = "frontend/src/wiring.ts"
doc = "The frontend's wiring."

[[wiring.slots]]
kind = "data"
name = "MENU"
point = "menu"
format = "ts"

[[wiring.slots]]
kind = "data"
name = "FLAGS"
point = "flags"
format = "ts"

[features.a]
summary = "A"

[[features.a.contributes.menu]]

[features.a.contributes.menu.data]
zone = "top"
label = "Café \\"A\\" \\\\ ✓"
href = "/a/b"
weight = 1e16
shown = true
tags = ["x", 2]
none = []
empty = {}

[features.a.contributes.menu.data.link]
rel = "next"
'''


def _data(text: str, features: tuple[str, ...], app: Contributions | None = None) -> str:
    catalog = _catalog(text)
    found = ordered(catalog, taken(catalog, select(catalog, features), app))
    return render_data(catalog, catalog.wiring[0], found)


def test_a_ts_data_file_is_a_comment_then_one_export_per_slot() -> None:
    assert _data(DATA, ("a",)) == (
        "// The frontend's wiring.\n"
        "//\n"
        "// Generated.\n"
        "// Don't edit.\n"
        "\n"
        'export const MENU = [{ "label": "Home", "href": "/", "feature": "core" }, '
        '{ "zone": "top", "label": "Café \\"A\\" \\\\ ✓", "href": "/a/b", "weight": 1e+16, "shown": true, '
        '"tags": ["x", 2], "none": [], "empty": {}, "link": { "rel": "next" }, "feature": "a" }] as const;\n'
        "export const FLAGS = [] as const;\n"
    )


def test_an_items_keys_stay_in_the_order_written_and_feature_comes_last() -> None:
    text = DATA.replace('{ label = "Home", href = "/" }', "{ z = 1, a = 2, m = 3 }")

    assert 'export const MENU = [{ "z": 1, "a": 2, "m": 3, "feature": "core" }] as const;\n' in _data(text, ())


def test_an_app_contribution_s_feature_is_app() -> None:
    app = project.load(
        "fastro.toml",
        'blueprint = { name = "data", version = "0.1.0" }\nfeatures = []\n[project]\nname = "p"\n'
        '[app]\ncontributes = { flags = [{ data = { name = "beta" } }] }\n',
    ).app.contributes

    assert 'export const FLAGS = [{ "name": "beta", "feature": "app" }] as const;\n' in _data(DATA, (), app)


def test_a_ts_doc_of_several_paragraphs_keeps_its_empty_lines_as_bare_comments() -> None:
    text = DATA.replace('doc = "The frontend\'s wiring."', 'doc = """\nThe wiring.\n\nSecond paragraph.\n"""')

    assert _data(text, ()).startswith("// The wiring.\n//\n// Second paragraph.\n//\n// Generated.\n")


def test_without_a_header_a_ts_comment_is_the_doc_alone() -> None:
    text = DATA.replace('[wiring_defaults]\nheader = """\nGenerated.\nDon\'t edit.\n"""\n', "")

    assert _data(text, ()).startswith("// The frontend's wiring.\n\nexport const MENU")


def test_a_json_data_file_is_the_object_of_every_slot_without_a_header() -> None:
    text = DATA.replace('format = "ts"', 'format = "json"').replace("wiring.ts", "wiring.json")

    assert _data(text, ()) == (
        "{\n"
        '  "MENU": [\n'
        "    {\n"
        '      "label": "Home",\n'
        '      "href": "/",\n'
        '      "feature": "core"\n'
        "    }\n"
        "  ],\n"
        '  "FLAGS": []\n'
        "}\n"
    )


def test_a_json_data_file_keeps_non_ascii_and_slashes_as_written() -> None:
    text = DATA.replace('format = "ts"', 'format = "json"').replace("wiring.ts", "wiring.json")

    rendered = _data(text, ("a",))

    assert '"label": "Café \\"A\\" \\\\ ✓",\n' in rendered
    assert '"href": "/a/b",\n' in rendered


def test_mini_s_data_wiring_renders_as_committed() -> None:
    mini = blueprint.load("blueprint.toml", MINI.read_text(), Version("0.1.0"))
    file = next(file for file in mini.wiring if file.path.text == "frontend/menu.ts")
    found = ordered(mini, taken(mini, select(mini, mini.presets["full"])))

    assert render_data(mini, file, found) == (MINI.parent / file.path.text).read_text()


def test_a_ts_item_keeps_a_non_ascii_key_as_written() -> None:
    text = DATA.replace('{ label = "Home", href = "/" }', '{ "café" = "/c" }')

    assert '[{ "café": "/c", "feature": "core" }]' in _data(text, ())


MINI_FILES = tuple(RepoPath(str(path.relative_to(MINI.parent))) for path in sorted(MINI.parent.rglob("*")) if path.is_file())


def _mini_plan(features: tuple[str, ...]) -> Plan:
    mini = blueprint.load("blueprint.toml", MINI.read_text(), Version("0.1.0"))
    return plan(mini, select(mini, features), MINI_FILES)


def test_rendering_mini_s_full_preset_gives_its_committed_wiring() -> None:
    mini = blueprint.load("blueprint.toml", MINI.read_text(), Version("0.1.0"))
    committed = {file.path.text: (MINI.parent / file.path.text).read_text() for file in mini.wiring}

    rendered = render(_mini_plan(tuple(mini.presets["full"])))

    assert rendered == committed
    assert list(rendered) == sorted(committed)


@pytest.mark.parametrize("features", [(), ("accounts",), ("billing", "cache")])
def test_render_gives_one_text_for_each_render_op_of_the_plan(features: tuple[str, ...]) -> None:
    made = _mini_plan(features)

    assert list(render(made)) == [op.path for op in made.ops if op.op == "render"]


def test_without_admin_its_wiring_files_arent_rendered() -> None:
    rendered = render(_mini_plan(("accounts",)))

    assert "backend/src/wiring/admin.py" in rendered
    assert "frontend/menu.ts" not in rendered


def test_render_takes_the_app_s_contributions_too() -> None:
    app = project.load(
        "fastro.toml",
        'blueprint = { name = "mini", version = "0.1.0" }\nfeatures = []\n[project]\nname = "a"\n'
        '[app]\ncontributes = { routers = [{ ref = "src.app_routes:router as app_router", prefix = "/app" }] }\n',
    ).app.contributes

    app_py = render(_mini_plan(()), app)["backend/src/wiring/app.py"]

    assert "from ..app_routes import router as app_router\n" in app_py
    assert 'ROUTERS: tuple[Mount, ...] = (Mount(app_router, "/app"),)\n' in app_py


def test_render_refuses_a_selection_that_gives_a_one_point_two_contributions() -> None:
    app = project.load(
        "fastro.toml",
        'blueprint = { name = "mini", version = "0.1.0" }\nfeatures = []\n[project]\nname = "a"\n'
        '[app]\ncontributes = { admin_site = ["src.app_site:site"] }\n',
    ).app.contributes

    with pytest.raises(TooManyContributions):
        render(_mini_plan(("admin",)), app)


def test_render_refuses_a_selection_that_leaves_a_fallback_less_slot_empty() -> None:
    text = MINI.read_text().replace('fallback = "src.admin_default:site"\n', "")
    mini = blueprint.load("blueprint.toml", text, Version("0.1.0"))

    with pytest.raises(NoContribution):
        render(plan(mini, select(mini, ()), MINI_FILES))


def test_a_render_error_is_a_value_error() -> None:
    assert issubclass(RenderError, ValueError)


def _app_shadows(contributes: str, features: tuple[str, ...] = ("accounts",)) -> list[tuple[tuple[str | int, ...], str]]:
    loaded = project.load(
        "fastro.toml",
        'blueprint = { name = "mini", version = "0.1.0" }\nfeatures = []\n[project]\nname = "a"\n'
        + (contributes if contributes.startswith("[[") else f"[app.contributes]\n{contributes}\n"),
    )
    made = _mini_plan(features)
    return shadows(made, loaded.app.contributes, loaded.document.line).app


APP_PY = "backend/src/wiring/app.py"


def test_an_app_ref_whose_name_its_wiring_file_binds_otherwise_shadows_it() -> None:
    assert _app_shadows('routers = [{ ref = "src.x:y as accounts_router", prefix = "/x" }]') == [
        (
            ("app", "contributes", "routers", 0),
            f"'src.x:y as accounts_router' binds 'accounts_router' in {APP_PY}, which that file binds otherwise too.",
        )
    ]


def test_an_app_ref_the_import_block_merges_with_a_feature_s_doesnt_shadow() -> None:
    contributes = 'routers = [{ ref = "src.modules.accounts.routes:router as accounts_router", prefix = "/x" }]'

    assert _app_shadows(contributes) == []


def test_of_two_app_refs_binding_one_name_the_later_one_shadows_naming_the_first_and_one_ref_twice_doesnt() -> None:
    contributes = (
        'routers = [{ ref = "src.x:y as mine", prefix = "/a" }, { ref = "src.x:y as mine", prefix = "/b" }]\n'
        'settings = ["src.z:Z as mine", "src.w:W as mine"]'
    )

    assert _app_shadows(contributes) == [
        (
            ("app", "contributes", "settings", index),
            f"{ref!r} binds 'mine' in {APP_PY}, which the app ref 'src.x:y as mine' binds too.",
        )
        for index, ref in enumerate(["src.z:Z as mine", "src.w:W as mine"])
    ]


def test_literal_data_and_reexport_points_are_never_shadowed() -> None:
    assert _app_shadows('task_modules = ["Mount"]\nmenu = [{ data = { href = "/", label = "Mount" } }]', ("admin",)) == []
    assert _app_shadows('admin_site = ["src.x:database"]', ()) == []


def test_the_later_of_two_app_refs_is_by_line_in_fastro_toml_not_by_point_or_slot() -> None:
    contributes = (
        '[[app.contributes.routers]]\nref = "src.x:y as other"\nprefix = "/a"\n'
        '[[app.contributes.settings]]\nref = "src.z:Z as mine"\n'
        '[[app.contributes.routers]]\nref = "src.w:W as mine"\nprefix = "/b"\n'
    )

    assert _app_shadows(contributes) == [
        (
            ("app", "contributes", "routers", 1),
            f"'src.w:W as mine' binds 'mine' in {APP_PY}, which the app ref 'src.z:Z as mine' binds too.",
        )
    ]


def test_an_app_ref_merging_with_a_feature_s_import_doesnt_hide_another_app_ref_that_shadows_it() -> None:
    contributes = (
        'routers = [{ ref = "src.health:database as accounts_router", prefix = "/a" }, '
        '{ ref = "src.modules.accounts.routes:router as accounts_router", prefix = "/b" }]'
    )

    assert _app_shadows(contributes) == [
        (
            ("app", "contributes", "routers", 0),
            f"'src.health:database as accounts_router' binds 'accounts_router' in {APP_PY}, which that file binds "
            "otherwise too.",
        ),
        (
            ("app", "contributes", "routers", 1),
            f"'src.modules.accounts.routes:router as accounts_router' binds 'accounts_router' in {APP_PY}, which the "
            "app ref 'src.health:database as accounts_router' binds too.",
        ),
    ]


def test_an_app_ref_that_shadows_a_reexport_another_app_ref_fills_shadows_it() -> None:
    text = MINI.read_text().replace(
        'fallback = "src.admin_default:site"\n',
        'fallback = "src.admin_default:site"\n\n[[wiring.slots]]\nkind = "list"\nname = "EXTRAS"\npoint = "extras"\n'
        'annotation = "list[object]"\n',
    )
    text += '\n[points.extras]\ncardinality = "many"\ndoc = "Extra things."\n'
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    made = plan(catalog, select(catalog, ()), MINI_FILES)

    def found(contributes: str) -> list[tuple[tuple[str | int, ...], str]]:
        loaded = project.load(
            "fastro.toml",
            'blueprint = { name = "mini", version = "0.1.0" }\nfeatures = []\n[project]\nname = "a"\n'
            f"[app.contributes]\n{contributes}\n",
        )
        return shadows(made, loaded.app.contributes, loaded.document.line).app

    assert found('admin_site = ["src.a:Y"]\nextras = ["src.b:SITE"]') == [
        (
            ("app", "contributes", "extras", 0),
            "'src.b:SITE' binds 'SITE' in backend/src/wiring/admin.py, which the app ref 'src.a:Y' binds too.",
        )
    ]
    assert found('admin_site = ["src.a:SITE"]\nextras = ["src.a:SITE"]') == []


def test_a_plain_import_in_a_wiring_file_is_a_binding_too() -> None:
    text = MINI.read_text().replace(
        'imports = ["from collections.abc import Callable", "from ..mount import Mount"]',
        'imports = ["import os", "from collections.abc import Callable", "from ..mount import Mount"]',
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    made = plan(catalog, select(catalog, ("accounts",)), MINI_FILES)

    def found(contributes: str) -> list[tuple[tuple[str | int, ...], str]]:
        loaded = project.load(
            "fastro.toml",
            'blueprint = { name = "mini", version = "0.1.0" }\nfeatures = []\n[project]\nname = "a"\n'
            f"[app.contributes]\n{contributes}\n",
        )
        return shadows(made, loaded.app.contributes, loaded.document.line).app

    assert found('routers = [{ ref = "src.x:y as mine", prefix = "/m" }]') == []
    assert found('routers = [{ ref = "src.x:y as os", prefix = "/m" }]') == [
        (("app", "contributes", "routers", 0), f"'src.x:y as os' binds 'os' in {APP_PY}, which that file binds otherwise too.")
    ]


def _catalog_shadows(text: str, features: tuple[str, ...], contributes: str = "") -> Shadows:
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    loaded = project.load(
        "fastro.toml",
        'blueprint = { name = "mini", version = "0.1.0" }\nfeatures = []\n[project]\nname = "a"\n'
        + (f"[app.contributes]\n{contributes}\n" if contributes else ""),
    )
    return shadows(plan(catalog, select(catalog, features), MINI_FILES), loaded.app.contributes, loaded.document.line)


ACCOUNTS_ROUTER = '"src.modules.accounts.routes:router as accounts_router"'


def test_of_two_catalog_refs_binding_one_name_the_later_in_blueprint_toml_shadows_naming_the_first() -> None:
    text = MINI.read_text().replace("router as billing_router", "router as accounts_router")

    assert _catalog_shadows(text, ("billing",)) == (
        [],
        [
            (
                ("features", "billing", "contributes", "routers", 0),
                f"'src.modules.billing.routes:router as accounts_router' binds 'accounts_router' in {APP_PY}, which "
                f"{ACCOUNTS_ROUTER[1:-1]!r} at features.accounts.contributes.routers[0] binds too.",
            )
        ],
    )


def test_a_catalog_ref_whose_name_its_file_binds_otherwise_shadows_it() -> None:
    text = MINI.read_text().replace(
        '"src.modules.cache.settings:CacheSettings"', '"src.modules.cache.settings:CacheSettings as Mount"'
    )

    assert _catalog_shadows(text, ("cache",)).catalog == [
        (
            ("features", "cache", "contributes", "settings", 0),
            f"'src.modules.cache.settings:CacheSettings as Mount' binds 'Mount' in {APP_PY}, which that file binds "
            "otherwise too.",
        )
    ]


def test_the_later_catalog_ref_is_by_line_in_blueprint_toml() -> None:
    core = '[core.contributes]\nhealth_checks = ["src.health:database"]\n'
    text = MINI.read_text().replace(core, "") + core + 'settings = ["src.other:CacheSettings"]\n'

    assert _catalog_shadows(text, ("cache",)).catalog == [
        (
            ("core", "contributes", "settings", 0),
            f"'src.other:CacheSettings' binds 'CacheSettings' in {APP_PY}, which 'src.modules.cache.settings:CacheSettings' "
            "at features.cache.contributes.settings[0] binds too.",
        )
    ]


def test_an_app_ref_filling_a_reexport_binds_the_slot_s_name_and_gives_way_to_the_catalog() -> None:
    text = MINI.read_text().replace(
        'fallback = "src.admin_default:site"\n',
        'fallback = "src.admin_default:site"\n\n[[wiring.slots]]\nkind = "list"\nname = "EXTRAS"\npoint = "extras"\n'
        'annotation = "list[object]"\n',
    )
    text = text.replace(
        'health_checks = ["src.health:database"]\n', 'health_checks = ["src.health:database"]\nextras = ["src.b:SITE"]\n'
    )
    text += '\n[points.extras]\ncardinality = "many"\ndoc = "Extra things."\n'

    assert _catalog_shadows(text, (), 'admin_site = ["src.a:Y"]') == (
        [
            (
                ("app", "contributes", "admin_site", 0),
                "'src.a:Y' binds 'SITE' in backend/src/wiring/admin.py, which that file binds otherwise too.",
            )
        ],
        [],
    )


HEALTH_PY = "backend/src/wiring/health.py"
HEALTH = 'health_checks = ["src.health:database"]'


def test_an_identical_app_ref_doesnt_hide_a_catalog_ref_that_shadows_the_file_s_import() -> None:
    text = MINI.read_text().replace(HEALTH, 'health_checks = ["src.health:database", "src.other:Callable"]')

    found = _catalog_shadows(text, (), 'health_checks = ["collections.abc:Callable"]')

    assert found.catalog == [
        (
            ("core", "contributes", "health_checks", 1),
            f"'src.other:Callable' binds 'Callable' in {HEALTH_PY}, which that file binds otherwise too.",
        )
    ]


def test_a_catalog_ref_identical_to_the_file_s_import_doesnt_take_the_blame_for_one_that_shadows_it() -> None:
    text = MINI.read_text().replace(
        HEALTH, 'health_checks = ["src.health:database", "src.other:Callable", "collections.abc:Callable"]'
    )

    assert _catalog_shadows(text, ()).catalog == [
        (
            ("core", "contributes", "health_checks", 1),
            f"'src.other:Callable' binds 'Callable' in {HEALTH_PY}, which that file binds otherwise too.",
        ),
        (
            ("core", "contributes", "health_checks", 2),
            f"'collections.abc:Callable' binds 'Callable' in {HEALTH_PY}, which 'src.other:Callable' at "
            "core.contributes.health_checks[1] binds too.",
        ),
    ]


def test_catalog_refs_on_one_line_are_ordered_as_written() -> None:
    core = '[core.contributes]\nhealth_checks = ["src.health:database"]\n'
    text = MINI.read_text().replace(core, "") + (
        '\n[core]\ncontributes = { health_checks = ["src.health:database"], settings = ["src.y:mine"], '
        'routers = [{ ref = "src.z:mine", prefix = "/z" }] }\n'
    )

    assert _catalog_shadows(text, ()).catalog == [
        (
            ("core", "contributes", "routers", 0),
            f"'src.z:mine' binds 'mine' in {APP_PY}, which 'src.y:mine' at core.contributes.settings[0] binds too.",
        )
    ]


def test_a_when_s_contribution_that_clashes_with_an_earlier_catalog_ref_is_the_one_reported() -> None:
    text = MINI.read_text().replace(
        'contributes = { email_sender = ["src.modules.accounts.email:send_queued"] }',
        'contributes = { email_sender = ["src.modules.accounts.email:send_queued"], '
        'routers = [{ ref = "src.w:accounts_router", prefix = "/w" }] }',
    )

    assert _catalog_shadows(text, ("accounts", "tasks")).catalog == [
        (
            ("features", "accounts", "when", 0, "contributes", "routers", 0),
            f"'src.w:accounts_router' binds 'accounts_router' in {APP_PY}, which "
            f"{ACCOUNTS_ROUTER[1:-1]!r} at features.accounts.contributes.routers[0] binds too.",
        )
    ]
