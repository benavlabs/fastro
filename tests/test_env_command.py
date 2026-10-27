"""``fastro env``: what it runs in the project, and what it says when it can't."""

import json
import os
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import fastro.cli
from fastro import plugins
from fastro.commands import env

runner = CliRunner()

UvCalls = Callable[[], dict[str, Any] | None]


@pytest.fixture
def uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UvCalls:
    """A ``uv`` on ``PATH`` that records how it was called, and exits with ``UV_EXIT``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "uv-calls.json"
    stub = bin_dir / "uv"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(calls)!r}, 'w') as f:\n"
        "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd()}, f)\n"
        "sys.exit(int(os.environ.get('UV_EXIT', '0')))\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    return lambda: json.loads(calls.read_text()) if calls.exists() else None


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with both environment scripts, entered at its root."""
    root = tmp_path / "acme"
    scripts = root / "backend" / "scripts"
    scripts.mkdir(parents=True)
    (root / "backend" / "pyproject.toml").write_text("[project]\nname = 'acme'\n")
    (scripts / "validate_env.py").write_text("")
    (scripts / "gen_secret.py").write_text("")
    monkeypatch.chdir(root)

    return root


def test_validate_runs_the_projects_validator_in_its_interpreter(project: Path, uv: UvCalls) -> None:
    result = runner.invoke(env.app, ["validate"])

    backend = project / "backend"
    assert result.exit_code == 0, result.output
    assert uv() == {
        "argv": ["run", "--project", str(backend.resolve()), "python", "-m", "scripts.validate_env"],
        "cwd": str(backend.resolve()),
    }


def test_validate_exits_with_the_validators_code(project: Path, uv: UvCalls, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UV_EXIT", "1")

    result = runner.invoke(env.app, ["validate"])

    calls = uv()
    assert calls is not None
    assert calls["argv"][-2:] == ["-m", "scripts.validate_env"]
    assert result.exit_code == 1


def test_secret_hands_the_byte_count_to_the_projects_script(project: Path, uv: UvCalls) -> None:
    result = runner.invoke(env.app, ["secret", "--bytes", "48"])

    assert result.exit_code == 0, result.output
    calls = uv()
    assert calls is not None
    assert calls["argv"][-4:] == ["-m", "scripts.gen_secret", "--bytes", "48"]


@pytest.mark.parametrize(("command", "script"), [("validate", "validate_env.py"), ("secret", "gen_secret.py")])
def test_a_project_without_the_script_is_told_so(project: Path, uv: UvCalls, command: str, script: str) -> None:
    (project / "backend" / "scripts" / script).unlink()

    result = runner.invoke(env.app, [command])

    assert result.exit_code == 3
    assert f"`fastro env {command}` is not available in this project" in result.stderr
    assert f"backend/scripts/{script}" in result.stderr
    assert result.stdout == ""
    assert uv() is None


def test_without_uv_the_command_says_what_is_missing(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    result = runner.invoke(env.app, ["validate"])

    assert result.exit_code == 3
    assert "`fastro env validate` runs in the project through uv, which isn't on PATH." in result.stderr
    assert "Install it: https://docs.astral.sh/uv/getting-started/installation/" in result.stderr
    assert result.stdout == ""


def test_validate_outside_a_project_says_where_to_run_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uv: UvCalls) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(env.app, ["validate"])

    assert result.exit_code == 1
    assert "run this from inside a project" in result.stderr
    assert uv() is None


def test_the_first_draw_is_printed_outside_a_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uv: UvCalls) -> None:
    """Nothing to ask: the rules live in the project, and the command still answers."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(env.app, ["secret", "--bytes", "20"])

    assert result.exit_code == 0
    assert len(bytes.fromhex(result.stdout.strip())) == 20
    assert uv() is None


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
