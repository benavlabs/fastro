"""The base snapshot in ``.fastro/base/files/`` and the ``[files]`` entries that go with it (spec 4)."""

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.apply import Change, apply
from fastro.engine.lock import Lock, LockedFile, Provenance
from fastro.engine.spec import RepoPath
from fastro.snapshot import ATTRIBUTES, SnapshotError, UnreadableSnapshot, read, recorded, snapshot

ORIGINS = {"a.png": "core", "backend/src/wiring/app.py": "wiring", "backend/pyproject.toml": "deps", "m/x.py": "feature:m"}
CHANGES = [
    Change("copy", "a.png"),
    Change("write", "backend/src/wiring/app.py", "ROUTERS = ()\n"),
    Change("write", "backend/pyproject.toml", '[project]\nname = "acmé"\r\n'),
    Change("copy", "m/x.py", source="m/template.py"),
    Change("copy", "backend/uv.lock", source="uv.lock"),
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "m").mkdir(parents=True)
    (root / "a.png").write_bytes(b"\x89PNG\r\n\x00\xff")
    (root / "m" / "template.py").write_text("x = 1\n")
    (root / "uv.lock").write_text("version = 1\n")
    return root


def test_each_copied_or_written_file_with_an_origin_is_recorded_with_the_hash_of_the_bytes_written(source: Path) -> None:
    assert recorded(CHANGES, ORIGINS, source) == {
        RepoPath("a.png"): LockedFile(_sha(b"\x89PNG\r\n\x00\xff"), "core"),
        RepoPath("backend/src/wiring/app.py"): LockedFile(_sha(b"ROUTERS = ()\n"), "wiring"),
        RepoPath("backend/pyproject.toml"): LockedFile(_sha('[project]\nname = "acmé"\r\n'.encode()), "deps"),
        RepoPath("m/x.py"): LockedFile(_sha(b"x = 1\n"), "feature:m"),
    }


def test_the_snapshot_mirrors_every_recorded_file_but_wiring_and_keeps_git_from_converting_it() -> None:
    assert snapshot(CHANGES, ORIGINS) == [
        Change("copy", ".fastro/base/files/a.png.base", source="a.png"),
        Change("write", ".fastro/base/files/backend/pyproject.toml.base", '[project]\nname = "acmé"\r\n'),
        Change("copy", ".fastro/base/files/m/x.py.base", source="m/template.py"),
        Change("write", ATTRIBUTES, "* -text\n"),
    ]
    assert ATTRIBUTES == ".fastro/.gitattributes"


def _project(source: Path, into: Path) -> Lock:
    apply([*CHANGES, *snapshot(CHANGES, ORIGINS)], source, into)
    return Lock(
        schema=1,
        fastro=Version("0.1.0"),
        blueprint=Provenance("mini", Version("0.1.0"), "../mini"),
        features=(),
        files=recorded(CHANGES, ORIGINS, source),
    )


def test_a_snapshot_file_reads_back_as_the_bytes_written(source: Path, tmp_path: Path) -> None:
    lock = _project(source, tmp_path / "acme")

    assert read(tmp_path / "acme", lock, "a.png") == b"\x89PNG\r\n\x00\xff"
    assert read(tmp_path / "acme", lock, "backend/pyproject.toml") == '[project]\nname = "acmé"\r\n'.encode()


@pytest.mark.parametrize(
    ("path", "damage", "message"),
    [
        ("a.png", lambda base: base.unlink(), ".fastro/base/files/a.png.base is missing"),
        ("a.png", lambda base: base.write_bytes(b"edited"), ".fastro/base/files/a.png.base doesn't match its sha256"),
        ("backend/src/wiring/app.py", lambda base: None, "backend/src/wiring/app.py is a wiring file, which has no snapshot"),
        ("nowhere.py", lambda base: None, "nowhere.py isn't in .fastro/lock.toml's [files]"),
    ],
)
def test_a_snapshot_file_that_s_missing_or_doesn_t_match_its_entry_is_refused_naming_it(
    source: Path, tmp_path: Path, path: str, damage: Callable[[Path], None], message: str
) -> None:
    lock = _project(source, tmp_path / "acme")
    damage(tmp_path / "acme" / ".fastro/base/files" / f"{path}.base")

    with pytest.raises(SnapshotError, match=re.escape(message)):
        read(tmp_path / "acme", lock, path)


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", ["outside", "fifo", "dangling", "directory"])
def test_a_snapshot_file_1_10_rules_out_is_a_usage_error_saying_why(
    source: Path, tmp_path: Path, replace: Callable[[Path, str, str], str], make: str
) -> None:
    lock = _project(source, tmp_path / "acme")
    reason = replace(tmp_path / "acme", ".fastro/base/files/m/x.py.base", make)

    with pytest.raises(UnreadableSnapshot, match=re.escape(f".fastro/base/files/m/x.py.base {reason}")):
        read(tmp_path / "acme", lock, "m/x.py")
