"""``fastro deploy generate``: the files it writes, byte for byte, against what ``b2c2900`` wrote."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

import fastro.cli

runner = CliRunner()

WOULD_WRITE = re.compile(r"^would write (?P<path>.+) \(\d+ bytes\)$", re.MULTILINE)

FIXTURES = Path(__file__).parent / "fixtures" / "deploy"

CASES = {
    "local": (["local"], False),
    "local-taskiq": (["local"], True),
    "local-flags": (["local", "--api-port", "9000"], False),
    "prod": (["prod"], False),
    "prod-taskiq": (["prod"], True),
    "prod-flags": (["prod", "--api-port", "9000", "--workers", "2"], False),
    "nginx": (["nginx"], False),
    "nginx-taskiq": (["nginx"], True),
    "nginx-flags": (["nginx", "--workers", "2", "--internal-subnet", "10.1.2.0/28"], False),
}


def _project(tmp_path: Path, *, taskiq: bool) -> Path:
    """An empty project named ``acme``, with the taskiq package when asked for."""
    project = tmp_path / "acme"
    (project / "backend").mkdir(parents=True)
    if taskiq:
        (project / "backend" / "src" / "infrastructure" / "taskiq").mkdir(parents=True)

    return project


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("case", list(CASES))
def test_generate_writes_what_b2c2900_wrote(tmp_path, case: str):
    args, taskiq = CASES[case]
    project = _project(tmp_path, taskiq=taskiq)

    result = runner.invoke(fastro.cli.app, ["deploy", "generate", *args, "-o", str(project), "--yes"])

    assert result.exit_code == 0, result.output
    assert _files(project) == _files(FIXTURES / case)


@pytest.mark.parametrize("case", ["local", "prod", "nginx"])
def test_a_dry_run_names_each_file_and_writes_none(tmp_path, case: str):
    args, taskiq = CASES[case]
    project = _project(tmp_path, taskiq=taskiq)

    result = runner.invoke(fastro.cli.app, ["deploy", "generate", *args, "-o", str(project), "--dry-run"])

    named = [match["path"] for match in WOULD_WRITE.finditer(result.stdout)]
    assert result.exit_code == 0, result.output
    assert named == [str(project.resolve() / path) for path in _files(FIXTURES / case)]
    assert "services:" not in result.output
    assert _files(project) == {}
