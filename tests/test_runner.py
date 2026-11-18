"""The declared-command runner (spec 2.9 ``[commands.*]``): which commands a selection has, and running one."""

import json
import shutil
import stat
import sys
from pathlib import Path

import pytest
from packaging.version import Version

from fastro import runner
from fastro.engine import blueprint
from fastro.engine.blueprint import Command
from fastro.engine.resolve import select
from fastro.engine.spec import RepoPath
from fastro.runner import OutsideProject, TokenUsageError, available, run_declared
from fastro.tools import MissingToolError, require

BOILERPLATE = Path(__file__).parent / "fixtures" / "fastro-boilerplate@e46bf26"
CATALOG = blueprint.load("blueprint.toml", (BOILERPLATE / "blueprint.toml").read_text(), Version("0.1.0"))
TOP = {("env", "secret"), ("env", "validate"), ("db", "baseline"), ("db", "tables"), ("db", "seed")}
BASELINE = Command(("db", "baseline"), "scripts.baseline")
SECRET = Command(("env", "secret"), "scripts.gen_secret", outside_project="token_hex")


@pytest.fixture
def uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``uv`` on ``PATH`` that records its argv and working directory, and exits with ``FAKE_UV_EXIT``."""
    record = tmp_path / "uv-calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'a') as calls:\n"
        "    calls.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
        "sys.exit(int(os.environ.get('FAKE_UV_EXIT', '0')))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    return record


def _calls(record: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def test_top_level_commands_are_always_available() -> None:
    assert set(available(CATALOG, select(CATALOG, ()))) == TOP


def test_a_feature_s_commands_come_with_the_feature() -> None:
    with_accounts = available(CATALOG, select(CATALOG, ("accounts",)))
    with_tiers = available(CATALOG, select(CATALOG, ("tiers",)))

    assert set(with_accounts) - TOP == {
        ("users", "unlock"),
        ("users", "canonicalize-emails"),
        ("users", "create-superuser"),
    }
    assert ("tiers", "create-default") in with_tiers and ("users", "unlock") in with_tiers
    assert ("tiers", "create-default") not in with_accounts


def test_available_maps_each_command_s_words_to_it() -> None:
    commands = available(CATALOG, select(CATALOG, ("accounts",)))

    assert commands[("db", "baseline")].module == "scripts.baseline"
    assert commands[("users", "unlock")].module == "scripts.unlock_account"


def test_a_declared_command_runs_through_uv_in_the_project_from_python_root(
    tmp_path: Path, uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "acme"
    (project / "backend").mkdir(parents=True)
    monkeypatch.setenv("FAKE_UV_EXIT", "7")

    code = run_declared(BASELINE, project, RepoPath("backend"), ["--check"])

    assert code == 7
    backend = str((project / "backend").resolve())
    assert _calls(uv) == [
        {"argv": ["run", "--project", backend, "python", "-m", "scripts.baseline", "--check"], "cwd": backend}
    ]


def test_a_command_s_cwd_is_where_it_runs_and_its_project_stays_python_root(tmp_path: Path, uv: Path) -> None:
    project = tmp_path / "acme"
    (project / "backend").mkdir(parents=True)
    (project / "frontend").mkdir()
    command = Command(("ui", "build"), "scripts.build_ui", cwd=RepoPath("frontend/"))

    assert run_declared(command, project, RepoPath("backend"), []) == 0

    (call,) = _calls(uv)
    assert call["cwd"] == str((project / "frontend").resolve())
    assert call["argv"] == ["run", "--project", str((project / "backend").resolve()), "python", "-m", "scripts.build_ui"]


def test_without_uv_a_declared_command_raises_missing_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "acme" / "backend").mkdir(parents=True)

    with pytest.raises(MissingToolError) as caught:
        run_declared(BASELINE, tmp_path / "acme", RepoPath("backend"), [])

    assert caught.value.tool == "uv"


@pytest.mark.parametrize(
    ("args", "length"), [([], 64), (["--bytes", "16"], 32), (["--bytes=48"], 96), (["--bytes", "128"], 256)]
)
def test_outside_a_project_a_token_hex_command_prints_a_token_without_running_anything(
    uv: Path, capsys: pytest.CaptureFixture[str], args: list[str], length: int
) -> None:
    assert run_declared(SECRET, None, RepoPath("backend"), args) == 0

    token = capsys.readouterr().out.strip()
    assert len(token) == length and int(token, 16) >= 0
    assert _calls(uv) == []


@pytest.mark.parametrize(
    "args",
    [
        ["--bytes", "15"],
        ["--bytes", "129"],
        ["--bytes", "x"],
        ["--bytes"],
        ["--length", "32"],
        ["--bytes", "32", "extra"],
        ["--bytes", "\u00b2"],
        ["--bytes", "\u0663\u0662"],
        ["--bytes=\u0663\u0662"],
    ],
)
def test_outside_a_project_a_token_hex_command_refuses_anything_but_bytes_from_16_to_128(uv: Path, args: list[str]) -> None:
    with pytest.raises(TokenUsageError, match="takes only --bytes N, an integer from 16 to 128"):
        run_declared(SECRET, None, RepoPath("backend"), args)

    assert _calls(uv) == []


def test_outside_a_project_any_other_command_is_refused(uv: Path) -> None:
    with pytest.raises(OutsideProject, match="fastro db baseline runs in a project"):
        run_declared(BASELINE, None, RepoPath("backend"), [])

    assert _calls(uv) == []


def test_inside_a_project_a_token_hex_command_runs_its_module_with_its_arguments(tmp_path: Path, uv: Path) -> None:
    (tmp_path / "acme" / "backend").mkdir(parents=True)

    assert run_declared(SECRET, tmp_path / "acme", RepoPath("backend"), ["--bytes", "48"]) == 0

    assert _calls(uv)[0]["argv"][-3:] == ["scripts.gen_secret", "--bytes", "48"]  # type: ignore[index]


def test_require_gives_a_tool_s_path_or_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/{name}" if name == "git" else None)

    assert require("git") == "/opt/git"
    with pytest.raises(MissingToolError, match="uv"):
        require("uv")


def test_a_relative_project_runs_with_absolute_paths_since_uv_starts_in_the_command_s_cwd(
    tmp_path: Path, uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "acme" / "backend").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    run_declared(BASELINE, Path("acme"), RepoPath("backend"), [])

    (call,) = _calls(uv)
    backend = str((tmp_path / "acme" / "backend").resolve())
    assert (call["argv"][2], call["cwd"]) == (backend, backend)  # type: ignore[index]


def test_run_captured_runs_from_cwd_with_only_the_environment_given_and_returns_code_and_output(tmp_path: Path) -> None:
    script = (
        "import os, sys; print(os.getcwd()); print(os.environ.get('ONLY', '-'), os.environ.get('HOME', 'none')); "
        "sys.stderr.write('err\\n'); sys.exit(3)"
    )
    code, output = runner.run_captured([sys.executable, "-c", script], tmp_path, {"ONLY": "given"})
    assert code == 3
    assert output == f"{tmp_path.resolve()}\ngiven none\nerr"


def test_run_captured_raises_when_the_program_can_t_be_started(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        runner.run_captured([str(tmp_path / "missing")], tmp_path, {})
