"""A3.8: every selection's generated Python is a fixed point of the pinned ruff, under the target project's config (2.8).

Every selection of a catalog, each subset of its features with each combination of option choices, is rendered.
Each distinct text of a path goes into its own copy of a backend tree carrying the project's ruff configuration, and
one ``ruff check`` and one ``ruff format --check`` run over all of them: neither may change anything.
"""

import itertools
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import plan
from fastro.engine.render import render
from fastro.engine.resolve import Selection, select

FIXTURES = Path(__file__).parent / "fixtures"
BOILERPLATE = FIXTURES / "fastro-boilerplate@e46bf26"
MINI = FIXTURES / "blueprints" / "mini"
RUFF = "ruff 0.15.12"
PACKAGES = ("src", "src/wiring", "scripts", "tests")


def _catalog(root: Path) -> Blueprint:
    return blueprint.load("blueprint.toml", (root / "blueprint.toml").read_text(), Version("0.1.0"))


def _every_selection(catalog: Blueprint) -> Iterator[Selection]:
    names = [feature.name for feature in catalog.features]
    keys = [(feature.name, option) for feature in catalog.features for option in feature.options]
    choices = [option.choices for feature in catalog.features for option in feature.options.values()]
    for size in range(len(names) + 1):
        for subset in itertools.combinations(names, size):
            for picked in itertools.product(*choices):
                yield select(catalog, subset, options=dict(zip(keys, picked, strict=True)))


def _distinct_python(catalog: Blueprint) -> dict[str, list[str]]:
    """Each rendered Python path, with every distinct text any selection gives it, in the order first rendered."""
    found: dict[str, dict[str, None]] = {}
    for selection in _every_selection(catalog):
        for path, text in render(plan(catalog, selection, ())).items():
            if path.endswith(".py"):
                found.setdefault(path, {})[text] = None
    return {path: list(texts) for path, texts in found.items()}


def _trees(into: Path, catalog: Blueprint, config: dict[str, str]) -> int:
    """Write the ``k``-th distinct text of each path into ``into/k/``, beside ``config``; return how many files."""
    python_root = catalog.info.python_root.text.rstrip("/")
    written = 0
    for path, texts in _distinct_python(catalog).items():
        for index, text in enumerate(texts):
            root = into / str(index)
            for package in PACKAGES:
                (root / python_root / package).mkdir(parents=True, exist_ok=True)
            for name, content in config.items():
                (root / python_root / name).write_text(content)
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
            written += 1
    return written


def _ruff(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "ruff", *arguments], cwd=cwd, capture_output=True, text=True)


CONFIGS = {
    "boilerplate": (BOILERPLATE, {"pyproject.toml": (BOILERPLATE / "backend-pyproject.toml").read_text()}),
    "mini": (MINI, {"pyproject.toml": (MINI / "backend" / "pyproject.toml").read_text(), "ruff.toml": ""}),
}


def test_the_ruff_these_tests_run_is_the_pinned_one() -> None:
    assert _ruff("--version", cwd=FIXTURES).stdout.strip() == RUFF


@pytest.mark.parametrize("name", list(CONFIGS))
def test_every_selection_s_python_wiring_passes_ruff_unchanged(tmp_path: Path, name: str) -> None:
    root, config = CONFIGS[name]
    written = _trees(tmp_path, _catalog(root), config)

    check = _ruff("check", "--no-cache", "--no-fix", ".", cwd=tmp_path)
    formatted = _ruff("format", "--no-cache", "--check", ".", cwd=tmp_path)

    assert written > 0
    assert check.returncode == 0, check.stdout + check.stderr
    assert formatted.returncode == 0, formatted.stdout + formatted.stderr
    assert formatted.stdout.strip().endswith(f"{written} files already formatted"), formatted.stdout
