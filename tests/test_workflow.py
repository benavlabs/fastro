"""CI runs the gate, on every Python fastro supports."""

from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"

GATE = [
    ".venv/bin/python -m ruff check src tests",
    ".venv/bin/python -m ruff format --check src tests",
    ".venv/bin/python -m mypy --strict src tests",
    ".venv/bin/python -m pytest -q",
]


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return workflow


def test_ci_runs_the_gate_in_order_after_installing_the_dev_extra() -> None:
    steps = [step["run"] for step in _workflow()["jobs"]["gate"]["steps"] if "run" in step]

    assert steps[-len(GATE) :] == GATE
    assert "uv pip install --python .venv/bin/python -e '.[dev]'" in steps[: -len(GATE)]


def test_ci_runs_on_every_python_from_3_11_to_3_15() -> None:
    job = _workflow()["jobs"]["gate"]

    assert job["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13", "3.14", "3.15"]
    assert "uv venv --python ${{ matrix.python }} .venv" in [step.get("run") for step in job["steps"]]


def test_ci_runs_on_pushes_to_main_and_on_pull_requests() -> None:
    triggers = _workflow()[True]

    assert triggers["push"]["branches"] == ["main"]
    assert "pull_request" in triggers


def test_ci_gets_a_token_that_can_only_read_the_repo() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}
