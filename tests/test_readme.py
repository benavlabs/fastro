"""The README's map of the package: every file it names exists, and every command it names runs."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

import fastro.cli

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "fastro"

runner = CliRunner()


def _tree() -> list[str]:
    """The lines of the README's ``src/fastro/`` tree."""
    readme = (ROOT / "README.md").read_text()
    (block,) = re.findall(r"```\nsrc/fastro/\n(.*?)```", readme, re.DOTALL)
    return block.splitlines()


def _named(line: str) -> str:
    return line.lstrip("│├└─ ").split()[0]


def _commands() -> list[list[str]]:
    found = []
    for line in _tree():
        for command in re.findall(r"fastro [a-z][a-z -]*[a-z]", line):
            found.append(command.split()[1:])
    return found


@pytest.mark.parametrize("name", [_named(line) for line in _tree()])
def test_every_file_the_readme_names_is_in_the_package(name: str):
    matches = [path for path in PACKAGE.rglob(name.rstrip("/")) if path.is_dir() == name.endswith("/")]

    assert matches, f"{name} is in the README's tree but not under src/fastro/"


@pytest.mark.parametrize("words", _commands(), ids=" ".join)
def test_every_command_the_readme_names_runs(words: list[str]):
    result = runner.invoke(fastro.cli.app, [*words, "--help"])

    assert result.exit_code == 0, result.output
