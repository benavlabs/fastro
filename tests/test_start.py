"""The installed ``fastro`` command refuses a current directory it can't use, before anything else runs (spec 1.37)."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

FASTRO = shutil.which("fastro", path=str(Path(sys.executable).parent))
COMMANDS = [["blueprint", "lint"], ["sync", "--check"], ["blueprint", "verify"], ["--help"]]
LAUNCHERS = ["script", "module"]


def _in(directory: Path, then: str, launcher: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run fastro from ``directory`` after the shell ``then`` has made it unusable (``chmod 0 .`` or ``rmdir``)."""
    assert FASTRO is not None, "the fastro console script isn't installed in this environment"
    fastro = [FASTRO] if launcher == "script" else [sys.executable, "-m", "fastro"]
    script = f'cd "$1" && {then} && shift && exec "$@"'
    return subprocess.run(
        ["sh", "-c", script, "sh", str(directory), *fastro, *command], capture_output=True, text=True, timeout=60
    )


def _locked_cwd_breaks_getcwd() -> bool:
    """Whether this platform's getcwd fails in a directory nobody may search: macOS yes, Linux no, root never."""
    with tempfile.TemporaryDirectory() as parent:
        locked = Path(parent) / "locked"
        locked.mkdir()
        try:
            probe = subprocess.run(
                ["sh", "-c", 'cd "$1" && chmod 0 . && exec "$2" -c "import os; os.getcwd()"', "sh", locked, sys.executable],
                capture_output=True,
                timeout=60,
            )
        finally:
            locked.chmod(0o755)
    return probe.returncode != 0


LOCKED_BREAKS_GETCWD = _locked_cwd_breaks_getcwd()


def _guard_message(reason: str) -> str:
    return f"error: fastro can't use the current directory: {reason}. Run it from a directory that exists and you can open.\n"


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_a_removed_current_directory_exits_3_with_fastro_s_message(tmp_path: Path, launcher: str, command: list[str]) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()

    done = _in(gone, 'rmdir "$1"', launcher, command)

    assert (done.returncode, done.stdout, done.stderr) == (3, "", _guard_message("No such file or directory"))


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_a_current_directory_nobody_may_search_exits_3_or_is_read_as_spec_1_37_says(
    tmp_path: Path, launcher: str, command: list[str]
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    try:
        done = _in(locked, "chmod 0 .", launcher, command)
    finally:
        locked.chmod(0o755)

    if LOCKED_BREAKS_GETCWD:
        assert (done.returncode, done.stdout, done.stderr) == (3, "", _guard_message("Permission denied"))
    elif command == ["--help"]:
        assert done.returncode == 0
        assert "Usage: " in done.stdout and "blueprint" in done.stdout
    else:
        name = "fastro.toml" if command[0] == "sync" else "blueprint.toml"
        assert done.returncode == 3
        assert f"error: can't read {locked / name}: Permission denied." in done.stderr
        assert "Traceback" not in done.stderr
