"""The suite's own guards: it stops at once as root, and a run with a skipped test fails, naming it."""

import importlib.util
import shutil
import subprocess
import sys
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


def _run_with_the_suite_s_conftest(tmp_path: Path, test: str) -> subprocess.CompletedProcess[str]:
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_one.py").write_text(test)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


def test_a_run_with_a_skipped_test_fails_naming_it(tmp_path: Path) -> None:
    done = _run_with_the_suite_s_conftest(
        tmp_path, "import pytest\n\ndef test_runs():\n    pass\n\n@pytest.mark.skip\ndef test_skipped():\n    pass\n"
    )

    assert done.returncode == 1, done.stdout
    assert "the suite allows no skipped tests, and 1 was skipped: test_one.py::test_skipped" in done.stdout


def test_a_module_skipped_whole_fails_the_run_too(tmp_path: Path) -> None:
    done = _run_with_the_suite_s_conftest(
        tmp_path, "import pytest\n\npytest.skip('not here', allow_module_level=True)\n\ndef test_runs():\n    pass\n"
    )

    assert done.returncode == 1, done.stdout
    assert "and 1 was skipped: test_one.py" in done.stdout


def test_a_run_with_no_skipped_test_passes(tmp_path: Path) -> None:
    done = _run_with_the_suite_s_conftest(tmp_path, "def test_runs():\n    pass\n")

    assert done.returncode == 0, done.stdout
    assert "no skipped tests" not in done.stdout
