"""A3.5: a project planned from mini under each preset, copied and rendered into a directory, passes its own tests."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import plan
from fastro.engine.render import render
from fastro.engine.resolve import Selection, select
from fastro.engine.spec import RepoPath

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
FILES = tuple(RepoPath(str(path.relative_to(MINI))) for path in sorted(MINI.rglob("*")) if path.is_file())


def _catalog() -> Blueprint:
    return blueprint.load("blueprint.toml", (MINI / "blueprint.toml").read_text(), Version("0.1.0"))


def _selection(catalog: Blueprint, name: str) -> Selection:
    if name == "paddle":
        return select(catalog, ("billing",), options={("billing", "provider"): "paddle"})
    return select(catalog, catalog.presets[name])


def _build(catalog: Blueprint, selection: Selection, root: Path) -> None:
    made = plan(catalog, selection, FILES)
    for op in made.ops:
        if op.op == "copy":
            (root / op.path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(MINI / op.path, root / op.path)
    for path, text in render(made).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


@pytest.mark.parametrize(("name", "passed"), [("minimal", 4), ("full", 11), ("paddle", 6)])
def test_a_planned_and_rendered_project_passes_its_backend_tests(tmp_path: Path, name: str, passed: int) -> None:
    catalog = _catalog()
    _build(catalog, _selection(catalog, name), tmp_path)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path / "backend",
        env=env,
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith(f"{passed} passed"), run.stdout


def test_the_project_holds_only_what_the_plan_copies_or_renders(tmp_path: Path) -> None:
    catalog = _catalog()
    selection = _selection(catalog, "minimal")
    _build(catalog, selection, tmp_path)
    made = {op.path for op in plan(catalog, selection, FILES).ops if op.op in ("copy", "render")}

    assert {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_file()} == made
