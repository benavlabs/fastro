"""Pytest settings for fastro's own suite, and the fixtures several test files share."""

import faulthandler
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

collect_ignore = ["fixtures"]

NOT_ROOT = "the permission tests assume a non-root user: run the suite as an ordinary user."


def pytest_sessionstart(session: pytest.Session) -> None:
    """Stop the run with one clear message as root, where a mode-000 file still opens and a dozen permission tests
    would fail for that one reason."""
    if os.geteuid() == 0:
        pytest.exit(NOT_ROOT, returncode=1)


REASONS = {
    "outside": "leads outside the repo",
    "fifo": "isn't a regular file",
    "dangling": "leads to nothing",
    "directory": "isn't a regular file",
    "back": "",
}


@pytest.fixture
def hang_guard() -> Iterator[None]:
    """Ends the test run, instead of waiting forever, if a FIFO gets opened."""
    faulthandler.dump_traceback_later(30, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture
def replace() -> Callable[[Path, str, str], str]:
    """Swaps a file in a repo's working tree for what spec 1.10 is about, and gives the reason fastro should name."""

    def swap(repo: Path, path: str, make: str) -> str:
        """``path`` becomes a symlink out of the repo to a copy of it, a FIFO, a symlink to nothing, a directory, or
        (``back``) a symlink that leaves the repo and comes back to a copy of it."""
        target = repo / path
        outside = repo.parent / "outside" / path
        text = target.read_bytes()
        target.unlink()
        if make in ("outside", "back"):
            outside.parent.mkdir(parents=True, exist_ok=True)
        if make == "outside":
            outside.write_bytes(text)
            target.symlink_to(outside)
        elif make == "fifo":
            os.mkfifo(target)
        elif make == "dangling":
            target.symlink_to(repo / "nothing-here")
        elif make == "directory":
            target.mkdir()
        else:
            real = repo / f"{path}.real"
            real.write_bytes(text)
            outside.symlink_to(real)
            target.symlink_to(outside)
        return REASONS[make]

    return swap
