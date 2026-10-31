"""``tests/fixtures/blueprints/mini``: the synthetic blueprint every engine task plans and renders against.

It's a blueprint repo, so it holds every path the catalog declares, whatever its own ``fastro.toml``
selects: ``billing/paddle.py`` is there although ``billing.provider`` is ``stripe``. The wiring in
``backend/src/wiring/`` is the ``full`` preset with the default options, as built.
"""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint, catalog_check, conditions
from fastro.engine.blueprint import SLOT_KEYS, Blueprint, Contributions
from fastro.engine.conditions import Condition
from fastro.engine.plan import plan
from fastro.engine.resolve import select
from fastro.engine.spec import RepoPath

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
QUEUED = "backend/tests/test_accounts_queued.py"


def _catalog() -> Blueprint:
    return blueprint.load("blueprint.toml", (MINI / "blueprint.toml").read_text(), Version("0.1.0"))


def _conditions(catalog: Blueprint) -> list[Condition]:
    groups: list[Contributions] = [catalog.core]
    found: list[Condition] = []
    for feature in catalog.features:
        groups.append(feature.contributes)
        for when in feature.when:
            found.append(when.condition)
            groups.append(when.contributes)
    found += [item.condition for group in groups for items in group.values() for item in items if item.condition]
    found += [file.condition for file in catalog.wiring if file.condition]
    found += [check.condition for check in catalog.verify.checks if check.condition]
    return found


def test_mini_and_its_fastro_toml_load(capsys: pytest.CaptureFixture[str]) -> None:
    assert catalog_check.main([str(MINI)]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == [
        f"ok: {MINI / 'blueprint.toml'} is mini 0.1.0, with 5 features, 7 points and 2 presets.",
        f"ok: {MINI / 'fastro.toml'} is mini, with 5 features, for mini 0.1.0.",
    ]


def test_the_catalogs_version_is_the_backends_project_version() -> None:
    project = tomllib.loads((MINI / "backend" / "pyproject.toml").read_text())["project"]

    assert Version(project["version"]) == _catalog().info.version


def test_every_path_the_catalog_declares_is_in_mini() -> None:
    catalog = _catalog()
    declared = [path for feature in catalog.features for path in feature.paths]
    declared += [path for feature in catalog.features for when in feature.when for path in when.paths]

    assert [path.text for path in declared if not (MINI / path.text).exists()] == []


def test_mini_covers_every_slot_kind_and_every_kind_of_condition() -> None:
    catalog = _catalog()
    slots = [slot for file in catalog.wiring for slot in file.slots]
    found = _conditions(catalog)
    atoms = [atom for condition in found for atom in conditions.atoms(condition)]
    contributions = [item for feature in catalog.features for items in feature.contributes.values() for item in items]

    assert {slot.kind for slot in slots} == set(SLOT_KEYS)
    assert any(slot.literal for slot in slots)
    assert any(isinstance(condition, conditions.Not) for condition in found)
    declared = {(feature.name, option) for feature in catalog.features for option in feature.options}
    assert declared <= {(atom.feature, atom.option) for atom in atoms if isinstance(atom, conditions.Option)}
    assert any(feature.when for feature in catalog.features)
    assert any(item.order is not None for item in contributions)
    assert any(file.condition is not None for file in catalog.wiring)


def test_one_value_point_is_fed_twice_by_one_owner_under_different_conditions() -> None:
    catalog = _catalog()
    values = {slot.point for file in catalog.wiring for slot in file.slots if slot.kind == "value"}
    fed: dict[tuple[str, str], set[Condition | None]] = {}
    for feature in catalog.features:
        groups = [(None, feature.contributes), *((when.condition, when.contributes) for when in feature.when)]
        for outer, group in groups:
            for point, items in group.items():
                for item in items if point in values else ():
                    fed.setdefault((feature.name, point), set()).add(item.condition or outer)

    assert any(len(found) > 1 for found in fed.values())


def test_the_backend_imports_and_passes_its_own_tests_without_writing_into_the_fixture() -> None:
    backend = MINI / "backend"
    before = sorted(path.relative_to(MINI) for path in MINI.rglob("*"))
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith("11 passed")
    assert sorted(path.relative_to(MINI) for path in MINI.rglob("*")) == before


@pytest.mark.parametrize(
    ("preset", "op", "reason"),
    [("minimal", "skip", "tasks doesn't hold"), ("full", "copy", "feature accounts is selected and tasks holds")],
)
def test_the_queued_email_test_comes_only_with_tasks(preset: str, op: str, reason: str) -> None:
    catalog = _catalog()
    files = tuple(RepoPath(str(path.relative_to(MINI))) for path in sorted(MINI.rglob("*")) if path.is_file())
    ops = {item.path: item for item in plan(catalog, select(catalog, catalog.presets[preset]), files).ops}

    assert (ops[QUEUED].op, ops[QUEUED].reason) == (op, reason)
