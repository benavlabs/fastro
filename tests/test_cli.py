"""The command names fastro answers to, as the product doc spells them."""

import re

import pytest
from typer.testing import CliRunner

import fastro.cli

runner = CliRunner()


@pytest.mark.parametrize("words", [["make", "deploy"], ["env", "secret"], ["env", "validate"]], ids=" ".join)
def test_the_command_exists(words: list[str]) -> None:
    result = runner.invoke(fastro.cli.app, [*words, "--help"])

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("words", [["deploy", "generate"], ["env", "gen-secret"]], ids=" ".join)
def test_the_old_spelling_is_a_usage_error(words: list[str]) -> None:
    result = runner.invoke(fastro.cli.app, [*words, "--help"])

    assert result.exit_code == 2
    assert "No such command" in result.output


def test_help_says_fastro_starts_and_maintains_fastapi_projects() -> None:
    result = runner.invoke(fastro.cli.app, ["--help"])

    assert result.exit_code == 0
    assert "Start and maintain FastAPI projects." in result.stdout
    assert re.search(r"\bFastro\b", result.stdout) is None
