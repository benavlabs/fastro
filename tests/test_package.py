"""The ``fastro`` wheel: what it's called, what it needs, and that it carries every package file."""

import importlib
import subprocess
import sys
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

import fastro

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "fastro"


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> zipfile.ZipFile:
    """The wheel the declared build backend makes from this checkout."""
    backend = tomllib.loads((ROOT / "pyproject.toml").read_text())["build-system"]["build-backend"]
    out = tmp_path_factory.mktemp("dist")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(ROOT)
        name = importlib.import_module(backend).build_wheel(str(out))

    return zipfile.ZipFile(out / name)


def _dist_info(wheel: zipfile.ZipFile, name: str) -> str:
    (path,) = [entry for entry in wheel.namelist() if entry.endswith(f".dist-info/{name}")]
    return wheel.read(path).decode()


def test_the_wheel_is_fastro_at_the_package_version(wheel):
    metadata = Parser().parsestr(_dist_info(wheel, "METADATA"))

    assert metadata["Name"] == "fastro"
    assert metadata["Version"] == "0.1.0.dev0"
    assert fastro.__version__ == "0.1.0.dev0"


def test_the_wheel_needs_no_backend(wheel):
    requires = Parser().parsestr(_dist_info(wheel, "METADATA")).get_all("Requires-Dist")
    runtime = sorted(req.split(">")[0].split("=")[0] for req in requires if "extra ==" not in req)

    assert runtime == ["jinja2", "typer"]


def test_the_wheel_installs_the_fastro_command(wheel):
    scripts = _dist_info(wheel, "entry_points.txt")

    assert "fastro = fastro.cli:app" in scripts.splitlines()


def test_the_wheel_carries_every_file_in_the_package_and_nothing_else(wheel):
    """The deploy templates and ``py.typed`` aren't Python, and a build can leave them behind."""
    packaged = {name for name in wheel.namelist() if ".dist-info/" not in name}
    on_disk = {
        path.relative_to(PACKAGE.parent).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }

    assert packaged == on_disk
    assert "fastro/py.typed" in packaged
    assert sum(name.endswith(".j2") for name in packaged) == 4


def test_python_dash_m_fastro_runs_the_cli():
    result = subprocess.run([sys.executable, "-m", "fastro", "--help"], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "Usage: python -m fastro" in result.stdout
