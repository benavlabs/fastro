"""The commands a blueprint declares, run where fastro finds ``fastro.toml`` (spec 2.9, A6)."""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from fastro import cli
from fastro.engine.resolve import SelectionError
from fastro.tools import NotInstalled

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
DECLARED = """
[commands."db baseline"]
module = "scripts.baseline"
help = "Write the baseline migration."

[commands."env secret"]
module = "scripts.gen_secret"
outside_project = "token_hex"

[features.admin.commands."admin unlock"]
module = "src.modules.admin.site"
help = "Unlock an admin account."
"""

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
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "mini"
    shutil.copytree(MINI, root)
    with (root / "blueprint.toml").open("a") as catalog:
        catalog.write(DECLARED)
    return root.resolve()


def _run(where: Path, *words: str) -> Result:
    return runner.invoke(cli.for_directory(where, list(words)), list(words))


def _declare(repo: Path, words: str, module: str = "scripts.other") -> None:
    with (repo / "blueprint.toml").open("a") as catalog:
        catalog.write(f'\n[commands."{words}"]\nmodule = "{module}"\n')


def test_a_declared_command_runs_its_module_in_the_project_with_every_argument(repo: Path, uv: UvCalls) -> None:
    given = ["--check", "--help", "x", "--", "--x=1", "--", "y"]

    result = _run(repo, "db", "baseline", *given)

    assert result.exit_code == 0, result.output
    assert uv() == {
        "argv": ["run", "--project", str(repo / "backend"), "python", "-m", "scripts.baseline", *given],
        "cwd": str(repo / "backend"),
    }


def test_a_declared_command_exits_with_its_modules_code(repo: Path, uv: UvCalls, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UV_EXIT", "7")

    assert _run(repo, "db", "baseline").exit_code == 7


def test_fastro_toml_is_found_from_a_directory_below_it(repo: Path, uv: UvCalls) -> None:
    result = _run(repo / "backend" / "src", "db", "baseline")

    assert result.exit_code == 0, result.output
    calls = uv()
    assert calls is not None
    assert calls["cwd"] == str(repo / "backend")


@pytest.mark.parametrize("words", [["db", "--help"], ["db"]], ids=["help", "nothing"])
def test_the_groups_help_lists_each_command_with_its_help(repo: Path, words: list[str]) -> None:
    result = _run(repo, *words)

    assert re.search(r"baseline\s+Write the baseline migration\.", re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)), result.output


@pytest.mark.parametrize(("features", "found"), [('"accounts", "admin"', True), ('"accounts"', False)])
def test_a_features_command_is_there_only_when_the_feature_is_selected(
    repo: Path, uv: UvCalls, features: str, found: bool
) -> None:
    toml = repo / "fastro.toml"
    toml.write_text(re.sub(r"features = \[.*\]", f"features = [{features}]", toml.read_text()))

    result = _run(repo, "admin", "unlock", "ada")

    assert (result.exit_code, uv() is not None) == ((0, True) if found else (2, False)), result.output
    if found:
        calls = uv()
        assert calls is not None
        assert calls["argv"][-2:] == ["src.modules.admin.site", "ada"]


def test_a_declared_env_secret_runs_in_the_project_in_place_of_the_built_in(repo: Path, uv: UvCalls) -> None:
    result = _run(repo, "env", "secret", "--bytes=48")

    assert result.exit_code == 0, result.output
    calls = uv()
    assert calls is not None
    assert calls["argv"][-2:] == ["scripts.gen_secret", "--bytes=48"]


def test_the_built_in_env_secret_runs_in_a_project_that_doesnt_declare_one(repo: Path, uv: UvCalls) -> None:
    catalog = repo / "blueprint.toml"
    catalog.write_text(catalog.read_text().replace('[commands."env secret"]', '[commands."env other"]'))

    result = _run(repo, "env", "secret", "--bytes", "20")

    assert result.exit_code == 0, result.output
    assert len(bytes.fromhex(result.stdout.strip())) == 20
    assert uv() is None


NOT_DECLARED = "error: this selection has no env validate command, so there's no validator to run.\n"
NO_COMMANDS = "error: this repo's declared commands aren't available, so there's no validator to run.\n"


@pytest.mark.parametrize(
    ("where", "reason"),
    [
        ("undeclared", NOT_DECLARED),
        ("unselected-feature", NOT_DECLARED),
        ("project", NO_COMMANDS),
        ("broken-catalog", NO_COMMANDS),
        ("extending", NO_COMMANDS),
        ("outside", "error: fastro env validate runs in a project. Run it from inside one.\n"),
    ],
)
def test_the_built_in_env_validate_is_a_usage_error_that_says_why(
    repo: Path, tmp_path: Path, uv: UvCalls, where: str, reason: str
) -> None:
    catalog, toml = repo / "blueprint.toml", repo / "fastro.toml"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    if where == "unselected-feature":
        with catalog.open("a") as appended:
            appended.write('\n[features.billing.commands."env validate"]\nmodule = "src.modules.billing.routes"\n')
        toml.write_text(re.sub(r"features = \[.*\]", 'features = ["accounts"]', toml.read_text()))
    if where == "project":
        toml.write_text(toml.read_text().replace("source = true\n", ""))
    if where == "broken-catalog":
        catalog.write_text(catalog.read_text().replace('module = "scripts.baseline"', 'module = "not a module"'))
    if where == "extending":
        _extend(repo, 'extends = { name = "base", version = "1.0.0" }')

    result = _run(outside if where == "outside" else repo, "env", "validate")

    assert (result.exit_code, result.stderr) == (2, reason)
    assert uv() is None


def test_outside_a_project_there_are_no_declared_commands(tmp_path: Path, uv: UvCalls) -> None:
    result = _run(tmp_path, "db", "baseline")

    assert result.exit_code == 2
    assert "No such command" in result.output
    assert uv() is None


@pytest.mark.parametrize("words", ["sync", "sync now", "make", "make deploy", "make thing", "blueprint lint", "env"])
def test_a_declared_command_that_shadows_a_built_in_is_ignored_with_a_warning(
    repo: Path, uv: UvCalls, capsys: pytest.CaptureFixture[str], words: str
) -> None:
    _declare(repo, words)

    result = _run(repo, "db", "baseline")

    assert result.exit_code == 0, result.output
    assert f"warning: blueprint.toml declares 'fastro {words}', which is built in; ignoring it.\n" == capsys.readouterr().err
    assert uv() is not None


def test_a_declared_command_under_a_built_in_group_runs(repo: Path, uv: UvCalls) -> None:
    _declare(repo, "env check", "scripts.check_env")

    result = _run(repo, "env", "check")

    assert result.exit_code == 0, result.output
    calls = uv()
    assert calls is not None
    assert calls["argv"][-1] == "scripts.check_env"
    assert _run(repo, "env", "secret", "--help").exit_code == 0


def test_a_declared_command_that_is_also_a_group_is_ignored_with_a_warning(
    repo: Path, uv: UvCalls, capsys: pytest.CaptureFixture[str]
) -> None:
    _declare(repo, "db", "scripts.db")

    result = _run(repo, "db", "baseline")

    assert result.exit_code == 0, result.output
    assert capsys.readouterr().err == (
        "warning: blueprint.toml declares 'fastro db' and commands under it; ignoring 'fastro db'.\n"
    )


def test_adding_the_declared_commands_leaves_the_built_in_app_as_it_was(repo: Path) -> None:
    _declare(repo, "env check", "scripts.check_env")
    _declare(repo, "blueprint thing", "scripts.thing")
    _declare(repo, "env more things", "scripts.more")

    built = cli.for_directory(repo, [])

    assert "thing" in runner.invoke(built, ["blueprint", "--help"]).stdout
    assert "check" not in runner.invoke(cli.app, ["env", "--help"]).stdout
    assert "more" not in runner.invoke(cli.app, ["env", "--help"]).stdout
    assert "thing" not in runner.invoke(cli.app, ["blueprint", "--help"]).stdout
    assert "No such command" in runner.invoke(cli.app, ["db", "baseline"]).output


def test_without_uv_a_declared_command_says_what_is_missing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    result = _run(repo, "db", "baseline")

    assert result.exit_code == 3
    assert "fastro db baseline runs in the project through uv, which isn't on PATH." in result.stderr


@pytest.mark.parametrize(
    ("words", "warned"),
    [
        (("db", "baseline"), True),
        (("env", "validate"), True),
        ((), True),
        (("sync",), False),
        (("blueprint", "lint"), False),
        (("make", "deploy"), False),
    ],
)
def test_a_catalog_with_problems_warns_that_its_commands_are_missing_unless_the_command_reports_it(
    repo: Path, capsys: pytest.CaptureFixture[str], words: tuple[str, ...], warned: bool
) -> None:
    catalog = repo / "blueprint.toml"
    catalog.write_text(catalog.read_text().replace('module = "scripts.baseline"', 'module = "not a module"'))

    cli.for_directory(repo, list(words))

    warning = (
        "warning: blueprint.toml has problems, so this repo's declared commands aren't available. "
        "fastro blueprint lint lists them.\n"
    )
    assert capsys.readouterr().err == (warning if warned else "")


def _extend(repo: Path, extends: str) -> None:
    catalog = repo / "blueprint.toml"
    old = 'requires_fastro = ">=0.1.0.dev0"'
    assert old in catalog.read_text()
    catalog.write_text(catalog.read_text().replace(old, f"{old}\n{extends}"))


@pytest.mark.parametrize(
    ("extends", "name"),
    [
        ('extends = { name = "base", version = "1.0.0" }', "base"),
        ('extends = { name = "base", version = "1.0.0" }\nbogus = 1', "another catalog"),
        ('extends = "base"', "another catalog"),
    ],
    ids=["named", "key-problem", "not-a-table"],
)
def test_an_extending_catalog_adds_no_declared_commands_and_warns_why(
    repo: Path, uv: UvCalls, capsys: pytest.CaptureFixture[str], extends: str, name: str
) -> None:
    _extend(repo, extends)

    result = _run(repo, "db", "baseline")

    assert result.exit_code == 2
    assert "No such command" in result.output
    assert capsys.readouterr().err == (
        f"warning: blueprint.toml extends {name}, and this fastro can't merge an extending catalog with its base yet. "
        "So this repo's declared commands aren't available.\n"
    )
    assert uv() is None


def test_a_project_that_isnt_a_blueprint_repo_warns_that_its_commands_wait(
    repo: Path, uv: UvCalls, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = repo / "fastro.toml"
    toml.write_text(toml.read_text().replace("source = true\n", ""))

    result = _run(repo, "db", "baseline")

    assert result.exit_code == 2
    assert capsys.readouterr().err == (
        "warning: fastro.toml doesn't set source = true, so this is a project, and declared commands in a "
        "project come with fastro new.\n"
    )


@pytest.mark.parametrize(
    "launch",
    [["-m", "fastro"], ["-c", "import sys; from fastro.compat import bp_main; sys.argv[0] = 'bp'; bp_main()"]],
    ids=["fastro", "bp"],
)
def test_the_console_scripts_hand_their_arguments_over_so_sync_alone_reports_a_broken_catalog(
    repo: Path, launch: list[str]
) -> None:
    catalog = repo / "blueprint.toml"
    catalog.write_text(catalog.read_text().replace('module = "scripts.baseline"', 'module = "not a module"'))

    done = subprocess.run([sys.executable, *launch, "sync", "--check"], cwd=repo, capture_output=True, text=True)

    assert done.returncode == 1, done.stderr
    assert "blueprint.toml:" in done.stderr
    assert "warning: blueprint.toml" not in done.stderr


@pytest.mark.parametrize(
    "launch",
    [["-m", "fastro"], ["-c", "import sys; from fastro.compat import bp_main; sys.argv[0] = 'bp'; bp_main()"]],
    ids=["fastro", "bp"],
)
def test_the_console_scripts_run_the_commands_declared_where_they_were_started(
    repo: Path, uv: UvCalls, launch: list[str]
) -> None:
    done = subprocess.run(
        [sys.executable, *launch, "db", "baseline", "--check"], cwd=repo / "backend", capture_output=True, text=True
    )

    assert done.returncode == 0, done.stderr
    calls = uv()
    assert calls is not None
    assert calls["argv"][-2:] == ["scripts.baseline", "--check"]


def test_a_declared_env_validate_runs_in_the_project_in_place_of_the_built_in(repo: Path, uv: UvCalls) -> None:
    _declare(repo, "env validate", "scripts.validate_env")
    built = cli.for_directory(repo, ["env", "validate"])
    env = next(group.typer_instance for group in built.registered_groups if group.name == "env")

    result = runner.invoke(built, ["env", "validate"])

    assert env is not None
    assert [command.name for command in env.registered_commands].count("validate") == 1

    assert result.exit_code == 0, result.output
    calls = uv()
    assert calls is not None
    assert calls["argv"][-1] == "scripts.validate_env"


def _unavailable(repo: Path, capsys: pytest.CaptureFixture[str]) -> str:
    result = _run(repo, "db", "baseline")
    assert result.exit_code == 2
    return capsys.readouterr().err


def test_a_fastro_toml_with_problems_says_so(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "fastro.toml").write_text((repo / "fastro.toml").read_text().replace('name = "mini"', 'name = "-"'))

    assert _unavailable(repo, capsys) == (
        "warning: fastro.toml has problems, so this repo's declared commands aren't available. "
        "fastro blueprint lint lists them.\n"
    )


def test_a_fastro_toml_naming_a_feature_the_catalog_doesnt_have_says_so(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    toml = repo / "fastro.toml"
    toml.write_text(re.sub(r"features = \[.*\]", 'features = ["nope"]', toml.read_text()))

    assert _unavailable(repo, capsys) == (
        "warning: fastro.toml has problems, so this repo's declared commands aren't available. "
        "fastro blueprint lint lists them.\n"
    )


def test_a_blueprint_repo_without_blueprint_toml_says_so(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "blueprint.toml").unlink()

    assert _unavailable(repo, capsys) == ("warning: fastro.toml sets source = true, but there's no blueprint.toml beside it.\n")


@pytest.mark.parametrize("name", ["fastro.toml", "blueprint.toml"])
def test_a_file_that_leads_out_of_the_repo_is_refused(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    outside = tmp_path / name
    (repo / name).rename(outside)
    (repo / name).symlink_to(outside)

    assert f"fastro reads {name} only as a regular file inside the repo." in _unavailable(repo, capsys)


def test_a_file_fastro_cant_read_says_so(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    catalog = repo / "blueprint.toml"
    catalog.chmod(0)
    try:
        warning = _unavailable(repo, capsys)
    finally:
        catalog.chmod(0o644)

    assert warning == (f"warning: can't read {catalog}: Permission denied, so the declared commands aren't available.\n")


def test_a_directory_fastro_cant_search_while_looking_for_fastro_toml_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0o600)
    try:
        result = _run(locked / "inner", "db", "baseline")
    finally:
        locked.chmod(0o755)

    assert result.exit_code == 2
    assert capsys.readouterr().err == (
        f"warning: can't look for fastro.toml in {locked / 'inner'}: Permission denied, so no declared commands are "
        "available.\n"
    )


def test_a_selection_fastro_cant_resolve_says_so(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_: object) -> None:
        raise SelectionError("Two features contribute to email_sender.")

    monkeypatch.setattr("fastro.declared.select", refuse)

    assert _unavailable(repo, capsys) == (
        "warning: Two features contribute to email_sender. So this repo's declared commands aren't available.\n"
    )


def test_a_fastro_that_isnt_installed_says_so(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing() -> None:
        raise NotInstalled("fastro isn't installed.")

    monkeypatch.setattr("fastro.sync.installed_version", missing)

    assert _unavailable(repo, capsys) == "warning: fastro isn't installed.\n"
