"""``bp``: it says it's now ``fastro``, then runs ``fastro`` with the old spellings translated."""

import subprocess
import sys
from pathlib import Path

import pytest

from fastro.compat import translate


def _bp(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the ``bp`` console script's entry point in a fresh interpreter."""
    script = "import sys; from fastro.compat import bp_main; sys.argv[0] = 'bp'; bp_main()"
    return subprocess.run([sys.executable, "-c", script, *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (["deploy", "generate", "local", "-o", "out"], ["make", "deploy", "local", "-o", "out"]),
        (["deploy", "generate", "--help"], ["make", "deploy", "--help"]),
        (["env", "gen-secret", "--bytes", "48"], ["env", "secret", "--bytes", "48"]),
        (["env", "validate"], ["env", "validate"]),
        (["--help"], ["--help"]),
        ([], []),
    ],
    ids=lambda argv: " ".join(argv) or "nothing",
)
def test_the_old_spellings_become_the_new_ones(old: list[str], new: list[str]) -> None:
    assert translate(old) == new


def test_bp_env_gen_secret_prints_only_the_key_on_stdout(tmp_path: Path) -> None:
    result = _bp("env", "gen-secret", "--bytes", "20", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert len(bytes.fromhex(result.stdout.strip())) == 20
    assert result.stderr == "bp is now fastro. Next time, run: fastro env secret --bytes 20\n"


def test_a_usage_error_keeps_its_exit_code(tmp_path: Path) -> None:
    result = _bp("deploy", "generate", "space", cwd=tmp_path)

    assert result.returncode == 2
    assert result.stderr.startswith("bp is now fastro. Next time, run: fastro make deploy space\n")
