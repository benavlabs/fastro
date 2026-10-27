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

    def install(name: str, group: str, body: str = "plugin = object()\n") -> Path:
        site = tmp_path / "site"
        marker = tmp_path / f"{name}.imported"
        (site / f"{name}-0.1.dist-info").mkdir(parents=True)
        (site / f"{name}-0.1.dist-info" / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1\n")
        (site / f"{name}-0.1.dist-info" / "entry_points.txt").write_text(f"[{group}]\n{name} = {name}:plugin\n")
        (site / f"{name}.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n{body}")
        monkeypatch.syspath_prepend(str(site))
        monkeypatch.delitem(sys.modules, name, raising=False)
        return marker

    return install


def test_a_bp_features_entry_point_is_never_loaded(installed: Install, tmp_path: Path) -> None:
    marker = installed("old_feature", "bp.features")
    project = tmp_path / "acme"
    (project / "backend").mkdir(parents=True)

    result = runner.invoke(fastro.cli.app, ["make", "deploy", "local", "-o", str(project), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert not marker.exists()


TYPER_PLUGIN = (
    "import typer\nplugin = typer.Typer()\n\n@plugin.command()\ndef hello():\n    typer.echo('hello from a plugin')\n"
)


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
