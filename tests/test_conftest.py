"""The suite's own guard: it stops at once, with one message, when it runs as root."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

CONFTEST = Path(__file__).parent / "conftest.py"


def _conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("suite_conftest", CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_suite_stops_at_once_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    conftest = _conftest()
    monkeypatch.setattr(conftest.os, "geteuid", lambda: 0)

    with pytest.raises(pytest.exit.Exception, match="the permission tests assume a non-root user") as stopped:
        conftest.pytest_sessionstart(None)
    assert stopped.value.returncode == 1


def test_the_suite_runs_as_an_ordinary_user(monkeypatch: pytest.MonkeyPatch) -> None:
    conftest = _conftest()
    monkeypatch.setattr(conftest.os, "geteuid", lambda: 1000)

    conftest.pytest_sessionstart(None)
