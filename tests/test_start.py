"""The installed ``fastro`` and ``bp`` commands exit 2 on native Windows (spec 8), and otherwise refuse a current
directory they can't use, before anything else runs (spec 1.37)."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

FASTRO = shutil.which("fastro", path=str(Path(sys.executable).parent))
BP = shutil.which("bp", path=str(Path(sys.executable).parent))
COMMANDS = [["blueprint", "lint"], ["sync", "--check"], ["blueprint", "verify"], ["--help"]]
LAUNCHERS = ["script", "module", "bp"]


def _in(directory: Path, then: str, launcher: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run fastro, through ``launcher``, from ``directory`` after the shell ``then`` has made it unusable
    (``chmod 0 .`` or ``rmdir``)."""
    assert FASTRO is not None and BP is not None, "the fastro and bp console scripts aren't installed here"
    fastro = {"script": [FASTRO], "module": [sys.executable, "-m", "fastro"], "bp": [BP]}[launcher]
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


def test_fastro_refuses_before_its_cli_is_even_imported() -> None:
    script = (
        "import os, sys\n"
        "def refuse():\n"
        "    raise PermissionError(13, 'Permission denied')\n"
        "os.getcwd = refuse\n"
        "from fastro import start\n"
        "try:\n"
        "    start.main()\n"
        "except SystemExit as stop:\n"
        "    print(stop.code, 'fastro.cli' in sys.modules)\n"
    )

    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)

    assert done.stdout == "3 False\n"


WINDOWS = "error: fastro doesn't support native Windows yet. Run it in WSL2: https://learn.microsoft.com/windows/wsl/install\n"


@pytest.mark.parametrize("command", [*COMMANDS, ["--version"], []])
@pytest.mark.parametrize("entry", ["fastro.start:main", "fastro.compat:bp_main"])
def test_native_windows_exits_2_before_reading_anything_or_loading_the_cli(
    tmp_path: Path, entry: str, command: list[str]
) -> None:
    module, function = entry.split(":")
    gone = tmp_path / "gone"
    gone.mkdir()
    script = (
        "import os, sys\n"
        f"os.rmdir({str(gone)!r})\n"
        f"from {module} import {function}\n"
        "sys.platform = 'win32'\n"
        "try:\n"
        f"    {function}()\n"
        "except SystemExit as stop:\n"
        "    print(stop.code, 'fastro.cli' in sys.modules)\n"
    )

    done = subprocess.run([sys.executable, "-c", script, *command], cwd=gone, capture_output=True, text=True, timeout=60)

    assert (done.returncode, done.stdout, done.stderr) == (0, "2 False\n", WINDOWS)
