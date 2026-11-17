"""``fastro blueprint verify``'s harness: which sets, each built through the engine, and the drill's report (A4 8a)."""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version
from typer.testing import CliRunner

from fastro.cli import app
from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.resolve import Selection, select
from fastro.verify import REPORTED_LINES, Result, UnknownSet, build, checks_for, report, verify, verify_sets

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
CATALOG = blueprint.load("blueprint.toml", (MINI / "blueprint.toml").read_text(), Version("0.1.0"))


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "mini"
    shutil.copytree(MINI, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_the_sets_are_the_declared_ones_then_the_presets_then_everything() -> None:
    assert verify_sets(CATALOG) == {
        "queues": ("tasks", "cache"),
        "minimal": ("accounts",),
        "full": ("accounts", "billing", "cache", "tasks", "admin"),
        "everything": ("accounts", "billing", "cache", "tasks", "admin"),
    }


def test_without_include_presets_the_presets_are_left_out() -> None:
    text = (MINI / "blueprint.toml").read_text().replace("[verify.sets]", "[verify]\ninclude_presets = false\n\n[verify.sets]")
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))

    assert list(verify_sets(catalog)) == ["queues", "everything"]


def test_a_build_is_what_the_plan_copies_and_renders_from_the_tracked_files(repo: Path, tmp_path: Path) -> None:
    (repo / "untracked.py").write_text("")

    project = build(repo, CATALOG, ("accounts",), tmp_path / "scratch" / "minimal")

    built = _tree(project)
    assert "backend/src/modules/accounts/routes.py" in built
    assert "backend/src/modules/billing/routes.py" not in built
    assert "frontend/menu.ts" not in built
    assert {"blueprint.toml", "fastro.toml", "untracked.py"}.isdisjoint(built)
    assert built["backend/src/wiring/app.py"].startswith(b'"""The app')


def test_a_build_never_writes_into_the_repo(repo: Path, tmp_path: Path) -> None:
    before = _tree(repo)

    build(repo, CATALOG, CATALOG.presets["full"], tmp_path / "scratch" / "full")

    assert _tree(repo) == before


def test_a_build_of_the_full_preset_carries_mini_s_committed_wiring(repo: Path, tmp_path: Path) -> None:
    project = build(repo, CATALOG, CATALOG.presets["full"], tmp_path / "full")

    for file in CATALOG.wiring:
        assert (project / file.path.text).read_bytes() == (MINI / file.path.text).read_bytes()


def test_a_passing_check_reports_its_last_line_only() -> None:
    assert report(Result("ruff", True, "All checks passed!", "lots\nAll checks passed!")) == (
        "  PASS  ruff         All checks passed!"
    )


def test_a_failing_check_reports_everything_it_printed_when_that_s_short() -> None:
    assert report(Result("tests", False, "1 failed", "E  boom\n1 failed")) == (
        "  FAIL  tests        1 failed\n        E  boom\n        1 failed"
    )


def test_a_long_failure_reports_its_end_and_says_what_it_dropped() -> None:
    printed = "\n".join(f"line {number}" for number in range(1, REPORTED_LINES + 6))

    lines = report(Result("mypy", False, f"line {REPORTED_LINES + 5}", printed)).splitlines()

    assert lines[0] == f"  FAIL  mypy         line {REPORTED_LINES + 5}"
    assert lines[1] == "        ... 5 earlier lines"
    assert lines[2] == "        line 6"
    assert len(lines) == REPORTED_LINES + 2


def _scratch_at(monkeypatch: pytest.MonkeyPatch, scratch: Path) -> None:
    def make(prefix: str) -> str:
        scratch.mkdir()
        return str(scratch)

    monkeypatch.setattr("fastro.verify.tempfile.mkdtemp", make)


def _checks(failing: set[str]) -> tuple[list[tuple[str, tuple[str, ...]]], object]:
    seen: list[tuple[str, tuple[str, ...]]] = []

    def run(project: Path, selection: Selection) -> list[Result]:
        seen.append((project.name, selection.features))
        assert (project / "backend/src/wiring/app.py").is_file()
        ok = project.name not in failing
        return [Result("app imports", ok, "fine" if ok else "ImportError: nope", "fine" if ok else "ImportError: nope")]

    return seen, run


def test_verify_builds_and_checks_each_set_asked_for_in_its_order(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seen, run = _checks(set())

    code = verify(repo, CATALOG, ["full", "queues"], run, keep=False)  # type: ignore[arg-type]

    assert code == 0
    assert seen == [("full", ("accounts", "billing", "cache", "tasks", "admin")), ("queues", ("cache", "tasks"))]
    assert capsys.readouterr().out == (
        "\n=== full: accounts, admin, billing, cache, tasks\n"
        "  PASS  app imports  fine\n"
        "\n=== queues: cache, tasks\n"
        "  PASS  app imports  fine\n"
    )


def test_with_no_set_named_verify_runs_every_set(repo: Path) -> None:
    seen, run = _checks(set())

    verify(repo, CATALOG, [], run, keep=False)  # type: ignore[arg-type]

    assert [name for name, _ in seen] == ["queues", "minimal", "full", "everything"]


def test_a_set_with_no_features_is_core_only(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = (
        (MINI / "blueprint.toml").read_text().replace('queues = ["tasks", "cache"]', 'queues = ["tasks", "cache"]\nbare = []')
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    _, run = _checks(set())

    verify(repo, catalog, ["bare"], run, keep=False)  # type: ignore[arg-type]

    assert capsys.readouterr().out.startswith("\n=== bare: core only\n")


def test_a_passing_run_removes_its_scratch_copies(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    _, run = _checks(set())

    assert verify(repo, CATALOG, ["minimal"], run, keep=False) == 0  # type: ignore[arg-type]

    assert not scratch.exists()


def test_keep_leaves_the_scratch_copies_and_says_where(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    _, run = _checks(set())

    verify(repo, CATALOG, ["minimal"], run, keep=True)  # type: ignore[arg-type]

    assert (scratch / "minimal" / "backend/src/wiring/app.py").is_file()
    assert capsys.readouterr().out.endswith(f"\nscratch copies left in {scratch}\n")


def test_a_failing_check_fails_the_run_and_leaves_the_scratch_copies(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    seen, run = _checks({"queues"})

    code = verify(repo, CATALOG, ["queues", "minimal"], run, keep=False)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert code == 1
    assert [name for name, _ in seen] == ["queues", "minimal"]
    assert "  FAIL  app imports  ImportError: nope\n        ImportError: nope\n" in out
    assert out.endswith(f"\nscratch copies left in {scratch}\n")
    assert (scratch / "queues").is_dir()


def test_a_build_that_raises_leaves_its_scratch_copy_behind_and_raises(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)

    def explode(project: Path, selection: Selection) -> list[Result]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        verify(repo, CATALOG, ["minimal"], explode, keep=False)

    assert scratch.exists()
    assert capsys.readouterr().out.endswith(f"\nscratch copies left in {scratch}\n")


def test_an_unknown_set_is_refused_before_anything_is_built(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fastro.verify.tempfile.mkdtemp", lambda prefix: pytest.fail("built"))
    _, run = _checks(set())

    with pytest.raises(UnknownSet, match="'nope' isn't a verify set. The sets are queues, minimal, full, everything."):
        verify(repo, CATALOG, ["full", "nope"], run, keep=False)  # type: ignore[arg-type]


def test_a_verify_set_named_like_a_preset_wins_over_it() -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace('queues = ["tasks", "cache"]', 'queues = ["tasks", "cache"]\nminimal = ["billing", "accounts"]')
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))

    assert verify_sets(catalog)["minimal"] == ("billing", "accounts")
    assert list(verify_sets(catalog)) == ["queues", "minimal", "full", "everything"]


APP_CATALOG = blueprint.load(
    "blueprint.toml",
    (MINI / "blueprint.toml").read_text().replace("[verify.sets]", '[verify]\napp = "src.app"\n\n[verify.sets]')
    + '\n[commands."db baseline"]\nmodule = "scripts.baseline"\n\n[checks.toolchain.env]\nSECRET_KEY = "verify-only"\n',
    Version("0.1.0"),
)


@pytest.fixture
def uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``uv`` that records argv, cwd and the environment it saw, prints a line, and fails a step on request."""
    record = tmp_path / "uv-calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "names = ('UV_PROJECT_ENVIRONMENT', 'VIRTUAL_ENV', 'SECRET_KEY', 'PYTHONPATH', 'PYTHONHOME')\n"
        "seen = {k: os.environ.get(k) for k in names}\n"
        f"with open({str(record)!r}, 'a') as calls:\n"
        "    calls.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': seen}) + '\\n')\n"
        "failing = os.environ.get('FAKE_UV_FAIL')\n"
        "if failing and any(failing in arg for arg in sys.argv[1:]):\n"
        "    print('\\n'.join(f'noise {n}' for n in range(3)))\n"
        "    print('boom', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print('fine')\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else/.venv")
    return record


def _calls(record: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def _built(repo: Path, tmp_path: Path, catalog: Blueprint, name: str) -> tuple[Path, Selection]:
    features = verify_sets(catalog)[name]
    return build(repo, catalog, features, tmp_path / "scratch" / name), select(catalog, features)


def test_the_built_in_checks_run_in_the_build_through_its_own_interpreter(repo: Path, tmp_path: Path, uv: Path) -> None:
    project, selection = _built(repo, tmp_path, APP_CATALOG, "minimal")

    results = checks_for(APP_CATALOG)(project, selection)

    assert [(result.name, result.ok, result.last_line) for result in results] == [
        ("app imports", True, "fine"),
        ("every module imports", True, "fine"),
        ("ruff", True, "fine"),
        ("mypy", True, "fine"),
        ("tests", True, "fine"),
        ("migrations", True, "fine"),
    ]
    backend = str((project / "backend").resolve())
    calls = _calls(uv)
    assert all(call["argv"][:4] == ["run", "--project", backend, "python"] for call in calls)
    assert all(Path(call["cwd"]).resolve() == Path(backend) for call in calls)
    assert calls[0]["argv"][4] == "-c" and calls[0]["argv"][5].endswith("import src.app")
    assert "pathlib.Path('src')" in calls[1]["argv"][5]
    assert calls[2]["argv"][4:] == ["-m", "ruff", "check", "src", "tests"]
    assert calls[3]["argv"][4:] == ["-m", "mypy", "src", "tests", "--config-file", "pyproject.toml"]
    assert calls[4]["argv"][4:] == ["-m", "pytest", "tests", "-q", "-p", "no:randomly"]
    assert calls[5]["argv"][4:] == ["-m", "scripts.baseline", "--check"]


def test_every_check_shares_one_environment_drops_virtual_env_and_gets_the_toolchain_env(
    repo: Path, tmp_path: Path, uv: Path
) -> None:
    project, selection = _built(repo, tmp_path, APP_CATALOG, "minimal")

    checks_for(APP_CATALOG)(project, selection)

    environment = str(tmp_path / "scratch" / ".venv")
    assert {json.dumps(call["env"]) for call in _calls(uv)} == {
        json.dumps(
            {
                "UV_PROJECT_ENVIRONMENT": environment,
                "VIRTUAL_ENV": None,
                "SECRET_KEY": "verify-only",
                "PYTHONPATH": None,
                "PYTHONHOME": None,
            }
        )
    }


def test_without_an_app_or_a_db_baseline_those_checks_are_skipped(repo: Path, tmp_path: Path, uv: Path) -> None:
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")

    results = checks_for(CATALOG)(project, selection)

    assert [result.name for result in results] == ["every module imports", "ruff", "mypy", "tests"]


def test_mypy_and_ruff_take_only_the_directories_the_build_has(repo: Path, tmp_path: Path, uv: Path) -> None:
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")
    for name in ("scripts", "migrations"):
        (project / "backend" / name).mkdir()

    checks_for(CATALOG)(project, selection)

    mypy = next(call for call in _calls(uv) if "mypy" in call["argv"])
    assert mypy["argv"][4:] == ["-m", "mypy", "src", "scripts", "migrations", "tests", "--config-file", "pyproject.toml"]


def test_a_failing_check_reports_what_it_printed(repo: Path, tmp_path: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_UV_FAIL", "ruff")
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")

    results = {result.name: result for result in checks_for(CATALOG)(project, selection)}

    assert (results["ruff"].ok, results["ruff"].last_line) == (False, "boom")
    assert results["ruff"].output == "noise 0\nnoise 1\nnoise 2\nboom"
    assert results["mypy"].ok


def test_a_verify_check_runs_from_its_cwd_only_when_its_if_holds(repo: Path, tmp_path: Path, uv: Path) -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace(
            'run = ["python", "-c", "pass"]\nif = "tasks"',
            f'run = ["{sys.executable}", "-c", "import os; print(os.getcwd())"]\ncwd = "frontend/"\nif = "admin"',
        )
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    full, with_admin = _built(repo, tmp_path, catalog, "full")
    minimal, without = _built(repo, tmp_path, catalog, "minimal")

    ran = checks_for(catalog)(full, with_admin)
    skipped = checks_for(catalog)(minimal, without)

    smoke = ran[-1]
    assert (smoke.name, smoke.ok) == ("smoke", True)
    assert Path(smoke.last_line).resolve() == (full / "frontend").resolve()
    assert "smoke" not in [result.name for result in skipped]


def test_a_verify_check_with_no_cwd_runs_from_the_build_root(repo: Path, tmp_path: Path, uv: Path) -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace(
            'run = ["python", "-c", "pass"]\nif = "tasks"', f'run = ["{sys.executable}", "-c", "import os; print(os.getcwd())"]'
        )
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    project, selection = _built(repo, tmp_path, catalog, "minimal")

    smoke = checks_for(catalog)(project, selection)[-1]

    assert Path(smoke.last_line).resolve() == project.resolve()


def test_a_verify_check_whose_program_is_missing_fails_instead_of_crashing(repo: Path, tmp_path: Path, uv: Path) -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace('run = ["python", "-c", "pass"]\nif = "tasks"', 'run = ["no-such-program-anywhere"]')
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    project, selection = _built(repo, tmp_path, catalog, "minimal")

    smoke = checks_for(catalog)(project, selection)[-1]

    assert (smoke.name, smoke.ok) == ("smoke", False)
    assert smoke.last_line == "no-such-program-anywhere isn't on PATH."


def _verify_cli(*arguments: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, ["blueprint", "verify", *arguments])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result.exit_code, result.stdout, result.stderr


def test_the_command_verifies_the_sets_asked_for_and_exits_0(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)

    code, out, _ = _verify_cli("minimal")

    assert code == 0
    assert out.startswith("\n=== minimal: accounts\n  PASS  every module imports fine\n")


def test_the_command_exits_1_when_a_check_fails(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("FAKE_UV_FAIL", "pytest")

    code, out, _ = _verify_cli("minimal")

    assert code == 1
    assert "  FAIL  tests        boom\n" in out
    assert "scratch copies left in " in out


def test_an_unknown_set_exits_2(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)

    code, _, err = _verify_cli("nope")

    assert code == 2
    assert "'nope' isn't a verify set" in err
    assert _calls(uv) == []


def test_without_uv_the_command_exits_3_before_building(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(repo)
    git = shutil.which("git")
    assert git is not None
    bin_dir = tmp_path / "only-git"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr("fastro.verify.tempfile.mkdtemp", lambda prefix: pytest.fail("built"))

    code, _, err = _verify_cli("minimal")

    assert code == 3
    assert "uv" in err


def test_a_directory_without_blueprint_toml_exits_2(tmp_path: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    code, _, err = _verify_cli()

    assert code == 2
    assert "has no blueprint.toml" in err


def test_a_catalog_with_problems_exits_1_and_points_at_lint(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    (repo / "blueprint.toml").write_text((repo / "blueprint.toml").read_text().replace("schema = 1", 'schema = "one"'))

    code, _, err = _verify_cli()

    assert code == 1
    assert "blueprint.toml:4: blueprint.schema: should be an integer, not a string." in err
    assert "fastro blueprint lint" in err


def test_a_build_without_tests_skips_the_tests_check(repo: Path, tmp_path: Path, uv: Path) -> None:
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")
    shutil.rmtree(project / "backend" / "tests")

    names = [result.name for result in checks_for(CATALOG)(project, selection)]

    assert "tests" not in names
    assert next(call for call in _calls(uv) if "ruff" in call["argv"])["argv"][4:] == ["-m", "ruff", "check", "src"]


def test_a_catalog_for_another_fastro_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    text = (repo / "blueprint.toml").read_text()
    (repo / "blueprint.toml").write_text(text.replace('requires_fastro = ">=0.1.0.dev0"', 'requires_fastro = ">=99"'))

    assert _verify_cli()[0] == 3


def test_an_unreadable_catalog_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    read = Path.read_bytes

    def refuse(path: Path) -> bytes:
        if path.name == "blueprint.toml":
            raise PermissionError(13, "Permission denied", str(path))
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", refuse)

    code, _, err = _verify_cli()

    assert code == 3
    assert "Permission denied" in err


def test_without_fastro_installed_verify_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)

    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)

    code, _, err = _verify_cli()

    assert code == 3
    assert "fastro isn't installed" in err


def test_a_directory_git_doesnt_track_exits_2(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    shutil.rmtree(repo / ".git")

    code, _, err = _verify_cli("minimal")

    assert code == 2
    assert "isn't a git repository" in err


def test_the_checks_never_see_a_pythonpath_or_pythonhome_from_the_parent_environment(
    repo: Path, tmp_path: Path, uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(repo / "backend"))
    monkeypatch.setenv("PYTHONHOME", "/somewhere/python")
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")

    checks_for(CATALOG)(project, selection)

    assert {(call["env"]["PYTHONPATH"], call["env"]["PYTHONHOME"]) for call in _calls(uv)} == {(None, None)}


def test_a_verify_check_whose_cwd_isnt_in_the_build_names_the_directory(repo: Path, tmp_path: Path, uv: Path) -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace('run = ["python", "-c", "pass"]\nif = "tasks"', f'run = ["{sys.executable}", "-c", "pass"]\ncwd = "nowhere/"')
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    project, selection = _built(repo, tmp_path, catalog, "minimal")

    smoke = checks_for(catalog)(project, selection)[-1]

    assert (smoke.name, smoke.ok) == ("smoke", False)
    assert smoke.last_line == f"Its cwd, {project / 'nowhere'}, isn't a directory in this build."


Replace = Callable[[Path, str, str], str]


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", ["outside", "fifo", "dangling", "directory", "back"])
def test_a_blueprint_toml_fastro_may_not_read_exits_2_saying_why_unless_it_comes_back(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, replace: Replace, make: str
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr("fastro.verify.tempfile.mkdtemp", lambda prefix: pytest.fail("built"))
    reason = replace(repo, "blueprint.toml", make)

    code, _, err = _verify_cli("nosuchset")

    assert code == 2
    if make == "back":
        assert "'nosuchset' isn't a verify set" in err
    else:
        message = f"error: {repo / 'blueprint.toml'} {reason}. fastro reads blueprint.toml only as a regular file inside"
        assert message in err
