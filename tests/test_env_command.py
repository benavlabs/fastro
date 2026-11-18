"""The built-in ``fastro env secret``, and where plugin warnings go."""

import warnings

import pytest
from typer.testing import CliRunner

import fastro.cli
from fastro import plugins
from fastro.commands import env

runner = CliRunner()


def test_the_built_in_secret_prints_a_token_of_the_bytes_asked_for() -> None:
    result = runner.invoke(env.app, ["secret", "--bytes", "20"])

    assert result.exit_code == 0
    assert len(bytes.fromhex(result.stdout.strip())) == 20


@pytest.mark.parametrize("given", ["15", "129", "many"])
def test_the_built_in_secret_refuses_a_size_outside_16_to_128(given: str) -> None:
    result = runner.invoke(env.app, ["secret", "--bytes", given])

    assert result.exit_code == 2
    assert result.stdout == ""


def test_a_plugin_warning_is_written_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """On stdout it would corrupt whatever the caller is piping command output into."""
    plugins.emit_plugin_warnings_to_stderr()

    warnings.showwarning("entry point 'acme' failed to load", RuntimeWarning, "plugins.py", 1)
    captured = capsys.readouterr()

    assert "acme" in captured.err
    assert captured.out == ""


def test_mounting_the_plugins_routes_their_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    routed: list[bool] = []
    monkeypatch.setattr(plugins, "emit_plugin_warnings_to_stderr", lambda: routed.append(True))

    fastro.cli._mount_command_plugins()

    assert routed == [True]
