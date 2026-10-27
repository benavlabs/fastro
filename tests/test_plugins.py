"""Which plugin entry points fastro loads, from distributions installed on ``sys.path``."""

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import fastro.cli

runner = CliRunner()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Install a distribution whose module leaves a marker file when it's imported."""

    def install(name: str, group: str) -> Path:
        site = tmp_path / "site"
        marker = tmp_path / f"{name}.imported"
        (site / f"{name}-0.1.dist-info").mkdir(parents=True)
        (site / f"{name}-0.1.dist-info" / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1\n")
        (site / f"{name}-0.1.dist-info" / "entry_points.txt").write_text(f"[{group}]\n{name} = {name}:plugin\n")
        (site / f"{name}.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nplugin = object()\n")
        monkeypatch.syspath_prepend(str(site))
        monkeypatch.delitem(sys.modules, name, raising=False)
        return marker

    return install


def test_a_bp_features_entry_point_is_never_loaded(installed, tmp_path):
    marker = installed("old_feature", "bp.features")
    project = tmp_path / "acme"
    (project / "backend").mkdir(parents=True)

    result = runner.invoke(fastro.cli.app, ["deploy", "generate", "local", "-o", str(project), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert not marker.exists()
