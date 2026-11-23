"""Which files fastro may read (spec 1.10), where the answer depends on how this Python reports a symlink loop."""

import errno
from pathlib import Path

import pytest

from fastro.engine.files import breaks_the_rule

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
