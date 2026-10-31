"""A3.6: track B's catalog renders each of the old removal drill's sets as the drill built it, compared by AST.

The drill's output is in ``fixtures/drill/`` (see its README). It used ``repr`` and its own layout, so files are
compared as B's own drill test compares them: no module docstring, imports as a set of statements, then every other
statement in order.
"""

import ast
import json
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import plan
from fastro.engine.render import render
from fastro.engine.resolve import select
from fastro.engine.spec import RepoPath

DRILL = Path(__file__).parent / "fixtures" / "drill"
SETS: dict[str, list[str]] = json.loads((DRILL / "sets.json").read_text())
CATALOG = blueprint.load("blueprint.toml", (DRILL / "blueprint.toml").read_text(), Version("0.1.0"))
TASKS = "backend/src/wiring/tasks.py"
EMAIL = "backend/src/wiring/email.py"
APP = "backend/src/wiring/app.py"


def _without_formatting(source: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    body = ast.parse(source).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    imports = sorted(ast.dump(node) for node in body if isinstance(node, ast.Import | ast.ImportFrom))
    statements = [ast.dump(node) for node in body if not isinstance(node, ast.Import | ast.ImportFrom)]
    return tuple(imports), tuple(statements)


def _render(catalog: Blueprint, features: list[str] | set[str]) -> dict[str, str]:
    return render(plan(catalog, select(catalog, sorted(features)), ()))


def _drill(name: str) -> dict[str, str]:
    return {str(path.relative_to(DRILL / name)): path.read_text() for path in sorted((DRILL / name).rglob("*.py"))}


def test_the_drill_has_its_nine_sets() -> None:
    assert len(SETS) == 9
    assert sorted(path.name for path in DRILL.iterdir() if path.is_dir()) == sorted(SETS)


def _requested(name: str) -> tuple[str, ...]:
    """What the catalog asks for in the set ``name``: its ``[verify.sets]`` entry, or every feature for ``everything``."""
    if name == "everything":
        return tuple(feature.name for feature in CATALOG.features)
    return CATALOG.verify.sets[name]


def test_the_catalog_s_features_require_what_the_drill_s_did() -> None:
    requires = json.loads((DRILL / "requires.json").read_text())

    assert {feature.name: sorted(feature.requires) for feature in CATALOG.features} == requires


def test_the_catalog_s_verify_sets_and_everything_are_the_drill_s_nine() -> None:
    assert sorted([*CATALOG.verify.sets, "everything"]) == sorted(SETS)


@pytest.mark.parametrize("name", list(SETS))
def test_the_catalog_closes_each_drill_set_as_the_drill_did(name: str) -> None:
    assert sorted(select(CATALOG, _requested(name)).features) == SETS[name]


@pytest.mark.parametrize("name", list(SETS))
def test_the_catalog_renders_the_files_the_drill_wrote(name: str) -> None:
    assert sorted(_render(CATALOG, SETS[name])) == sorted(_drill(name))


@pytest.mark.parametrize(
    ("name", "path"), [(name, path) for name in SETS for path in _drill(name)], ids=lambda value: str(value)
)
def test_each_rendered_file_is_the_drill_s_by_ast(name: str, path: str) -> None:
    assert _without_formatting(_render(CATALOG, SETS[name])[path]) == _without_formatting(_drill(name)[path])


def test_the_comparison_tells_two_sets_apart() -> None:
    assert _without_formatting(_drill("core-only")[APP]) != _without_formatting(_render(CATALOG, SETS["everything"])[APP])


@pytest.mark.parametrize("chosen", [{"accounts", "api_keys"}, {"accounts", "taskiq"}, {"taskiq"}, set()])
def test_the_usage_prune_is_registered_by_neither_feature_alone(chosen: set[str]) -> None:
    assert "src.modules.api_keys.tasks" not in _render(CATALOG, chosen)[TASKS]


def test_the_usage_prune_is_registered_where_both_features_are_present() -> None:
    assert "src.modules.api_keys.tasks" in _render(CATALOG, {"accounts", "api_keys", "taskiq"})[TASKS]


@pytest.mark.parametrize(
    ("features", "path", "absent"),
    [
        (("accounts", "api_keys"), "backend/src/modules/api_keys/tasks.py", True),
        (("accounts", "cache", "taskiq"), "backend/src/modules/api_keys/service.py", True),
        (("accounts", "api_keys", "taskiq"), "backend/src/modules/api_keys/tasks.py", False),
    ],
)
def test_a_task_module_comes_only_with_its_feature_and_the_worker(features: tuple[str, ...], path: str, absent: bool) -> None:
    (op,) = [op for op in plan(CATALOG, select(CATALOG, features), (RepoPath(path),)).ops if op.path == path]

    assert (op.op == "skip") == absent, op


def test_a_project_with_a_worker_queues_the_message() -> None:
    assert "EMAIL_SENDER: EmailSender = queued_sender\n" in _render(CATALOG, {"accounts", "taskiq"})[EMAIL]


def test_a_project_without_one_hands_the_delivery_over() -> None:
    wired = _render(CATALOG, {"accounts"})[EMAIL]

    assert "from ..infrastructure.email.senders import HandingOverSender\n" in wired
    assert "EMAIL_SENDER: EmailSender = HandingOverSender(build_sender())\n" in wired


def test_a_project_without_one_drains_at_shutdown() -> None:
    wired = _render(CATALOG, {"accounts"})[APP]

    assert "from ..infrastructure.handover import lifecycle as handover_lifecycle\n" in wired
    assert "handover_lifecycle" in wired.split("LIFECYCLES")[1]


def test_a_project_with_a_worker_drains_before_it_closes_the_broker() -> None:
    wired = _render(CATALOG, {"accounts", "taskiq"})[APP]

    assert "handover_lifecycle" not in wired
    assert "taskiq_lifecycle" in wired.split("LIFECYCLES")[1]
