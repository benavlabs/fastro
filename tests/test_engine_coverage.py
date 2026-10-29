"""The engine modules that parse catalog text get 100% branch coverage from their own tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fastro.engine

ENGINE = Path(fastro.engine.__file__).parent
TESTS = Path(__file__).parent

FULLY_COVERED = {
    "blueprint": (
        "test_engine_blueprint.py",
        "test_engine_blueprint_command.py",
        "test_engine_blueprint_wiring.py",
        "test_engine_blueprint_tools.py",
        "test_engine_references.py",
        "test_engine_project.py",
        "test_engine_lock.py",
    ),
    "catalog_check": ("test_engine_blueprint_command.py", "test_engine_references.py"),
    "conditions": ("test_engine_conditions.py",),
    "lock": ("test_engine_lock.py",),
    "positions": ("test_engine_positions.py",),
    "problems": ("test_engine_problems.py",),
    "project": ("test_engine_project.py",),
    "references": ("test_engine_references.py", "test_engine_project.py"),
}


@pytest.mark.parametrize(("module", "tests"), sorted(FULLY_COVERED.items()))
def test_the_module_has_full_branch_coverage(module: str, tests: tuple[str, ...], tmp_path: Path) -> None:
    env = {**os.environ, "COVERAGE_FILE": str(tmp_path / ".coverage")}
    source = str(ENGINE / f"{module}.py")
    coverage = [sys.executable, "-m", "coverage"]
    measure = ["run", "--branch", f"--include={source}"]
    pytest_args = ["-m", "pytest", "-q", "-p", "no:cacheprovider", *(str(TESTS / name) for name in tests)]

    run = subprocess.run([*coverage, *measure, *pytest_args], env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr

    report = tmp_path / "coverage.json"
    subprocess.run([*coverage, "json", "-o", str(report)], env=env, capture_output=True, check=True)
    files = json.loads(report.read_text())["files"]
    missing = {path: (summary["missing_lines"], summary["missing_branches"]) for path, summary in files.items()}

    assert [str(Path(path).resolve()) for path in files] == [source]
    assert [summary["summary"]["percent_covered"] for summary in files.values()] == [100.0], missing
