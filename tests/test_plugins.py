"""Which plugin entry points fastro loads, from distributions installed on ``sys.path``."""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import fastro.cli

runner = CliRunner()

Install = Callable[..., Path]


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    """Install a distribution whose module leaves a marker file when it's imported."""

    def install(name: str, group: str, body: str = "plugin = object()\n", module: str | None = None) -> Path:
        module = module or name
        site = tmp_path / "site"
        marker = tmp_path / f"{module}.imported"
        (site / f"{module}-0.1.dist-info").mkdir(parents=True)
        (site / f"{module}-0.1.dist-info" / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {module}\nVersion: 0.1\n")
        (site / f"{module}-0.1.dist-info" / "entry_points.txt").write_text(f"[{group}]\n{name} = {module}:plugin\n")
        (site / f"{module}.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n{body}")
        monkeypatch.syspath_prepend(str(site))
        monkeypatch.delitem(sys.modules, module, raising=False)
        return marker

    return install


def test_a_bp_features_entry_point_is_never_loaded(installed: Install, tmp_path: Path) -> None:
    marker = installed("old_feature", "bp.features")
    project = tmp_path / "acme"
    (project / "backend").mkdir(parents=True)

    result = runner.invoke(fastro.cli.app, ["make", "deploy", "local", "-o", str(project), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert not marker.exists()


def typer_plugin(says: str = "hello from a plugin") -> str:
    """A plugin module whose one command, ``hello``, prints ``says``."""
    return f"import typer\nplugin = typer.Typer()\n\n@plugin.command()\ndef hello():\n    typer.echo({says!r})\n"


TYPER_PLUGIN = typer_plugin()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> typer.Typer:
    """The root app, with any group a test mounts dropped again afterwards."""
    monkeypatch.setattr(fastro.cli.app, "registered_groups", [*fastro.cli.app.registered_groups])
    return fastro.cli.app


@pytest.mark.parametrize("name", ["make", "env"])
def test_a_plugin_cannot_take_a_built_in_name(
    installed: Install, app: typer.Typer, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    installed(name, "bp.commands", TYPER_PLUGIN)

    fastro.cli._mount_command_plugins()

    assert f"plugin command '{name}' shadows a built-in" in capsys.readouterr().err
    assert [group.name for group in app.registered_groups].count(name) == 1


def test_a_plugin_can_take_a_name_fastro_no_longer_uses(installed: Install, app: typer.Typer) -> None:
    installed("deploy", "bp.commands", TYPER_PLUGIN)

    fastro.cli._mount_command_plugins()
    result = runner.invoke(app, ["deploy", "hello"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "hello from a plugin\n"


def test_a_fastro_commands_plugin_mounts_without_a_warning(
    installed: Install, app: typer.Typer, capsys: pytest.CaptureFixture[str]
) -> None:
    installed("greet", "fastro.commands", TYPER_PLUGIN)

    fastro.cli._mount_command_plugins()
    result = runner.invoke(app, ["greet", "hello"])

    assert result.stdout == "hello from a plugin\n"
    assert capsys.readouterr().err == ""


def test_a_bp_commands_plugin_mounts_and_is_told_to_move(
    installed: Install, app: typer.Typer, capsys: pytest.CaptureFixture[str]
) -> None:
    installed("greet", "bp.commands", TYPER_PLUGIN)

    fastro.cli._mount_command_plugins()
    result = runner.invoke(app, ["greet", "hello"])

    assert result.stdout == "hello from a plugin\n"
    assert (
        "warning: plugin 'greet' is registered under bp.commands, which fastro 0.2 stops reading; "
        "register it under fastro.commands instead."
    ) in capsys.readouterr().err


def test_a_plugin_in_both_groups_is_loaded_once_from_fastro_commands(
    installed: Install, app: typer.Typer, capsys: pytest.CaptureFixture[str]
) -> None:
    installed("greet", "fastro.commands", typer_plugin("from fastro.commands"), module="greet_new")
    old = installed("greet", "bp.commands", typer_plugin("from bp.commands"), module="greet_old")

    fastro.cli._mount_command_plugins()
    result = runner.invoke(app, ["greet", "hello"])

    assert result.stdout == "from fastro.commands\n"
    assert not old.exists()
    assert [group.name for group in app.registered_groups].count("greet") == 1
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ("raise RuntimeError('boom')\n", "warning: Failed to load plugin 'broken' from 'broken:plugin': boom"),
        ("plugin = object()\n", "warning: Plugin 'broken' resolved to object, expected typer.Typer; ignoring."),
    ],
    ids=["raises", "not-a-typer"],
)
def test_a_broken_plugin_warns_and_the_rest_still_mount(
    installed: Install, app: typer.Typer, capsys: pytest.CaptureFixture[str], body: str, says: str
) -> None:
    installed("broken", "fastro.commands", body)
    installed("greet", "fastro.commands", TYPER_PLUGIN)

    fastro.cli._mount_command_plugins()
    result = runner.invoke(app, ["greet", "hello"])

    assert says in capsys.readouterr().err
    assert result.stdout == "hello from a plugin\n"
    assert runner.invoke(app, ["make", "deploy", "--help"]).exit_code == 0
