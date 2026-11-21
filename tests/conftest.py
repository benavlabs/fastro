"""Pytest settings for fastro's own suite, and the fixtures several test files share."""

import faulthandler
import importlib
import os
import pkgutil
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import fastro
from fastro.engine import problems

collect_ignore = ["fixtures"]

NOT_ROOT = "the permission tests assume a non-root user: run the suite as an ordinary user."


def pytest_sessionstart(session: pytest.Session) -> None:
    """Stop the run with one clear message as root, where a mode-000 file still opens and a dozen permission tests
    would fail for that one reason."""
    if os.geteuid() == 0:
        pytest.exit(NOT_ROOT, returncode=1)


SKIPPED: list[str] = []
INTERNAL = re.compile(
    r"(?<![\w.\"])(?:wiring|modules|plugin|starter|feature\.when|verify\.checks)\[\d|\]\.slots\[\d"
    r"|(?<![\w.\"])features\.(?:[\w-]+|\"[^\"]*\")\.when\[\d"
)
NAMED_BY_POSITION: list[str] = []


def _watch(cls: type) -> None:
    """Note each ``cls`` made during the run whose text names an entry of an array of tables by its position."""
    original = getattr(cls, "__init__")

    def init(self: object, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        if INTERNAL.search(str(self)):
            NAMED_BY_POSITION.append(str(self))

    setattr(cls, "__init__", init)


for module in pkgutil.walk_packages(fastro.__path__, "fastro."):
    if not module.name.endswith("__main__"):
        importlib.import_module(module.name)


def _errors(cls: type) -> Iterator[type]:
    sub: type
    for sub in cls.__subclasses__():
        if sub.__module__.startswith("fastro."):
            yield sub
        yield from _errors(sub)


_watch(problems.Problem)
for error in dict.fromkeys(_errors(BaseException)):
    _watch(error)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Note each skipped test; an expected failure isn't one."""
    if report.skipped and not hasattr(report, "wasxfail"):
        SKIPPED.append(report.nodeid)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Note a module skipped whole while it's collected."""
    if report.skipped:
        SKIPPED.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a run that skipped a test, on every Python version: the suite allows none. Fail one that made an error
    naming an entry of an array of tables by its position, which spec 1.11 rules out."""
    if (SKIPPED or NAMED_BY_POSITION) and exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Name the skipped tests, and the errors naming an entry by its position, that failed the run."""
    for text in dict.fromkeys(NAMED_BY_POSITION):
        terminalreporter.write_line(f"an error names an entry of an array of tables by its position (1.11): {text}")
    if SKIPPED:
        count = len(SKIPPED)
        names = ", ".join(SKIPPED)
        terminalreporter.write_line(
            f"the suite allows no skipped tests, and {count} {'was' if count == 1 else 'were'} skipped: {names}"
        )


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
