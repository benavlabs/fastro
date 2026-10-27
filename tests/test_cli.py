"""The command names fastro answers to, as the product doc spells them."""

import pytest
from typer.testing import CliRunner

import fastro.cli

runner = CliRunner()


@pytest.mark.parametrize("words", [["make", "deploy"], ["env", "secret"], ["env", "validate"]], ids=" ".join)
def test_the_command_exists(words: list[str]):
    result = runner.invoke(fastro.cli.app, [*words, "--help"])

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("words", [["deploy", "generate"], ["env", "gen-secret"]], ids=" ".join)
def test_the_old_spelling_is_a_usage_error(words: list[str]):
    result = runner.invoke(fastro.cli.app, [*words, "--help"])

    assert result.exit_code == 2
    assert "No such command" in result.output
