"""Which files fastro may read (spec 1.10), where the answer depends on how this Python reports a symlink loop."""

import errno
from pathlib import Path

import pytest

from fastro.engine.files import breaks_the_rule


def test_a_symlink_loop_reported_as_eloop_breaks_the_rule_as_one_reported_as_a_runtime_error_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve = Path.resolve

    def looping(self: Path, strict: bool = False) -> Path:
        if self.name == "blueprint.toml":
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(self))
        return resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", looping)

    assert breaks_the_rule(tmp_path, "blueprint.toml") == "has symlinks that can't be followed"
