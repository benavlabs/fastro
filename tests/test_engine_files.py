"""Which files fastro may read (spec 1.10), where the answer depends on how this Python reports a symlink loop."""

import errno
from pathlib import Path

import pytest

from fastro.engine.files import breaks_the_rule, shown, utf8

LOOPS = [
    RuntimeError("Symlink loop from 'blueprint.toml'"),
    OSError(errno.ELOOP, "Too many levels of symbolic links", "blueprint.toml"),
]


@pytest.mark.parametrize("loop", LOOPS, ids=["python-3.11-and-3.12", "python-3.13-on"])
def test_a_symlink_loop_breaks_the_rule_however_this_python_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop: Exception
) -> None:
    resolve = Path.resolve

    def looping(self: Path, strict: bool = False) -> Path:
        if self.name == "blueprint.toml":
            raise loop
        return resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", looping)

    assert breaks_the_rule(tmp_path, "blueprint.toml") == "has symlinks that can't be followed"


@pytest.mark.parametrize(("path", "expected"), [("backend/café.py", True), ("backend/caf\udce9.py", False), ("a", True)])
def test_a_path_is_utf_8_unless_it_holds_a_byte_utf_8_can_t_decode(path: str, expected: bool) -> None:
    assert utf8(path) is expected


def test_a_path_is_shown_with_each_byte_utf_8_can_t_decode_written_as_an_escape() -> None:
    assert shown(b"d\xe9/caf\xc3(.txt".decode("utf-8", "surrogateescape")) == "d\\xe9/caf\\xc3(.txt"
    assert shown("backend/café.py") == "backend/café.py"
