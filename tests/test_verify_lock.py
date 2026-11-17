"""A verify build starts from the blueprint's ``uv.lock``, ``{python_root}``'s first, and prints which (spec 2.13, A15 1)."""

import base64
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.apply import ApplyError, tracked_files
from fastro.engine import blueprint
from fastro.engine.resolve import select
from fastro.engine.spec import RepoPath
from fastro.verify import build, checks_for, starting_lock, verify

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
TEXT = (MINI / "blueprint.toml").read_text()
LOCKED_OUT = 'requires_fastro = ">=0.1.0.dev0"\nblueprint_only = ["pyproject.toml", "uv.lock", "backend/uv.lock"]'
CATALOG = blueprint.load("blueprint.toml", TEXT.replace('requires_fastro = ">=0.1.0.dev0"', LOCKED_OUT), Version("0.1.0"))


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "mini"
    shutil.copytree(MINI, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


def _status(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout


def _track(repo: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        (repo / name).write_bytes(data)
    _git(repo, "add", *files)


@pytest.mark.parametrize(
    ("locks", "start"),
    [(["uv.lock"], "uv.lock"), (["backend/uv.lock"], "backend/uv.lock"), (["uv.lock", "backend/uv.lock"], "backend/uv.lock")],
)
def test_a_build_starts_from_python_root_s_lock_or_else_the_root_s(
    repo: Path, tmp_path: Path, locks: list[str], start: str
) -> None:
    _track(repo, {name: f"# {name}\r\nversion = 1\n".encode() for name in locks})
    before = _status(repo)

    project = build(repo, CATALOG, (), tmp_path / "core")

    assert starting_lock(CATALOG, tracked_files(repo)) == RepoPath(start)
    assert (project / "backend/uv.lock").read_bytes() == (repo / start).read_bytes()
    assert not (project / "uv.lock").exists()
    assert _status(repo) == before


def test_an_untracked_lock_isnt_started_from(repo: Path, tmp_path: Path) -> None:
    (repo / "uv.lock").write_text("version = 1\n")

    project = build(repo, CATALOG, (), tmp_path / "core")

    assert starting_lock(CATALOG, tracked_files(repo)) is None
    assert not (project / "backend/uv.lock").exists()


def test_a_python_root_lock_that_is_a_symlink_refuses_the_build_without_trying_the_root_s(repo: Path, tmp_path: Path) -> None:
    _track(repo, {"uv.lock": b"version = 1\n"})
    (repo / "backend/uv.lock").symlink_to("../uv.lock")
    _git(repo, "add", "backend/uv.lock")

    with pytest.raises(ApplyError, match="'backend/uv.lock' is a symlink in the source"):
        build(repo, CATALOG, (), tmp_path / "core")

    assert not (tmp_path / "core").exists()


@pytest.mark.parametrize(("locks", "line"), [([], "  lock: none"), (["uv.lock"], "  lock: uv.lock")])
def test_verify_prints_the_lock_each_build_started_from_or_none(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], locks: list[str], line: str
) -> None:
    _track(repo, {name: b"version = 1\n" for name in locks})
    monkeypatch.setattr("fastro.verify.tempfile.mkdtemp", lambda prefix: str(tmp_path / "scratch"))

    code = verify(repo, CATALOG, ["minimal", "queues"], lambda project, selection: [], keep=False)

    assert code == 0
    assert capsys.readouterr().out == f"\n=== minimal: accounts\n{line}\n\n=== queues: cache, tasks\n{line}\n"


def _wheel(into: Path, version: str) -> None:
    info = f"pinned-{version}.dist-info"
    files = {
        "pinned/__init__.py": f'VERSION = "{version}"\n',
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: pinned\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = [
        f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b'=').decode()},"
        f"{len(text.encode())}"
        for name, text in files.items()
    ]
    with zipfile.ZipFile(into / f"pinned-{version}-py3-none-any.whl", "w") as wheel:
        for name, text in files.items():
            wheel.writestr(name, text)
        wheel.writestr(f"{info}/RECORD", "\n".join([*record, f"{info}/RECORD,,"]) + "\n")


@pytest.mark.parametrize(("locked", "installed"), [(True, "1.0"), (False, "2.0")], ids=["from-the-lock", "no-lock"])
def test_a_build_that_starts_from_the_workspace_lock_cant_pick_up_a_newer_release(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: bool, installed: str
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "the suite needs uv on PATH"
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "1.0")
    for name, value in {
        "UV_FIND_LINKS": str(wheels),
        "UV_NO_INDEX": "1",
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_PYTHON": sys.executable,
    }.items():
        monkeypatch.setenv(name, value)
    workspace = (
        '[project]\nname = "ws"\nversion = "0"\nrequires-python = ">=3.11"\n\n[tool.uv.workspace]\nmembers = ["backend"]\n'
    )
    backend = (
        '[project]\nname = "mini-backend"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = ["pinned>=1"]\n'
        "\n[tool.uv]\npackage = false\n"
    )
    (repo / "pyproject.toml").write_text(workspace)
    (repo / "backend/pyproject.toml").write_text(backend)
    subprocess.run([uv, "lock", "--directory", str(repo)], check=True, capture_output=True)
    _git(repo, "add", "pyproject.toml", "backend/pyproject.toml", "uv.lock")
    _wheel(wheels, "2.0")
    tracked = tuple(path for path in tracked_files(repo) if locked or path.text != "uv.lock")
    before = _status(repo)

    project = build(repo, CATALOG, (), tmp_path / "core", tracked)
    assert _status(repo) == before
    install = checks_for(CATALOG)(project, select(CATALOG, ()))[0]

    assert (install.name, install.ok) == ("install", True), install.output
    venv_python = project.parent / ".venv" / "bin" / "python"
    shown = subprocess.run([str(venv_python), "-c", "import pinned; print(pinned.VERSION)"], capture_output=True, text=True)
    assert shown.stdout.strip() == installed
