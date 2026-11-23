"""``fastro blueprint verify``'s harness: which sets, each built through the engine, and the drill's report (A4 8a)."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version
from typer.testing import CliRunner

from fastro import verify as verify_module
from fastro.apply import ApplyError, tracked_files
from fastro.cli import app
from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.render import RenderError
from fastro.engine.resolve import Selection, select
from fastro.engine.spec import RepoPath
from fastro.verify import REPORTED_LINES, Result, Unbuildable, UnknownSet, build, checks_for, report, verify, verify_sets

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
CATALOG = blueprint.load("blueprint.toml", (MINI / "blueprint.toml").read_text(), Version("0.1.0"))

EXTENDS: dict[str, tuple[str, str]] = {
    "named": ('extends = { name = "base", version = "1.0.0" }', "base"),
    "key-problem": ('extends = { name = "base", version = "1.0.0" }\nbogus = 1', "another catalog"),
    "not-a-table": ('extends = "base"', "another catalog"),
    "no-version": ('extends = { name = "base" }', "another catalog"),
}
EXTENDING = "error: blueprint.toml extends {name}, and this fastro can't merge an extending catalog with its base yet.\n"


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

    monkeypatch.setattr("fastro.verify.mkdtemp", make)


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
        "  lock: none\n"
        "  PASS  app imports  fine\n"
        "\n=== queues: cache, tasks\n"
        "  lock: none\n"
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

    assert capsys.readouterr().out.startswith("\n=== bare: core only\n  lock: none\n")


OPTIONED = (
    'queues = { features = ["tasks", "billing", "cache"], '
    'options = { "cache.backend" = "redis", "billing.provider" = "paddle" } }'
)
REDIS_ONLY = (
    'if = "cache.backend=redis"\nservices = ["redis"]\n',
    'if = "cache.backend=redis"\nservices = ["redis"]\n\n[features.cache.when.contributes]\n'
    'settings = ["src.modules.cache.redis:RedisSettings"]\n',
)


def test_a_set_s_options_reach_its_build_its_checks_and_its_report(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = (MINI / "blueprint.toml").read_text().replace('queues = ["tasks", "cache"]', OPTIONED).replace(*REDIS_ONLY)
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    seen = []

    def run(project: Path, selection: Selection) -> list[Result]:
        seen.append((selection.options, "RedisSettings" in (project / "backend/src/wiring/app.py").read_text()))
        return [Result("app imports", True, "fine", "fine")]

    verify(repo, catalog, ["queues", "full"], run, keep=False)

    assert seen[0] == ({("billing", "provider"): "paddle", ("cache", "backend"): "redis"}, True)
    assert seen[1] == ({("billing", "provider"): "stripe", ("cache", "backend"): "memory"}, False)
    out = capsys.readouterr().out
    assert out.startswith(
        "\n=== queues: accounts, billing, cache, tasks\n  option: cache.backend=redis\n  option: billing.provider=paddle\n"
        "  lock: none\n  PASS  app imports  fine\n\n=== full: accounts, admin, billing, cache, tasks\n  lock: none\n"
    )


def test_a_set_written_as_a_table_with_a_preset_s_name_builds_with_its_own_features_and_options(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shadow = '\nminimal = { features = ["cache"], options = { "cache.backend" = "redis" } }'
    text = (MINI / "blueprint.toml").read_text().replace('queues = ["tasks", "cache"]', 'queues = ["tasks", "cache"]' + shadow)
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    seen = []

    def run(project: Path, selection: Selection) -> list[Result]:
        seen.append((selection.features, selection.options))
        return [Result("app imports", True, "fine", "fine")]

    verify(repo, catalog, ["minimal"], run, keep=False)

    assert seen == [(("cache",), {("cache", "backend"): "redis"})]
    assert capsys.readouterr().out.startswith("\n=== minimal: cache\n  option: cache.backend=redis\n  lock: none\n")


def test_a_set_written_as_a_table_without_options_builds_with_the_defaults(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace('queues = ["tasks", "cache"]', 'queues = { features = ["tasks", "cache"] }')
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    seen, run = _checks(set())

    verify(repo, catalog, ["queues"], run, keep=False)  # type: ignore[arg-type]

    assert seen == [("queues", ("cache", "tasks"))]
    assert capsys.readouterr().out.startswith("\n=== queues: cache, tasks\n  lock: none\n")


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
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))
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
        ("install", True, "fine"),
        ("app imports", True, "fine"),
        ("every module imports", True, "fine"),
        ("ruff", True, "fine"),
        ("mypy", True, "fine"),
        ("tests", True, "fine"),
        ("migrations", True, "fine"),
    ]
    backend = str((project / "backend").resolve())
    install, *calls = _calls(uv)
    assert install["argv"] == ["sync", "--project", backend]
    assert Path(install["cwd"]).resolve() == Path(backend)
    assert all(call["argv"][:4] == ["run", "--project", backend, "python"] for call in calls)
    assert all(Path(call["cwd"]).resolve() == Path(backend) for call in calls)
    assert calls[0]["argv"][4] == "-c" and calls[0]["argv"][5].endswith("import src.app")
    assert "pathlib.Path('src')" in calls[1]["argv"][5]
    assert calls[2]["argv"][4:] == ["-m", "ruff", "check", "src", "tests"]
    assert calls[3]["argv"][4:] == ["-m", "mypy", "src", "tests", "--config-file", "pyproject.toml"]
    assert calls[4]["argv"][4:] == ["-m", "pytest", "tests", "-q", "-p", "no:randomly"]
    assert calls[5]["argv"][4:] == ["-m", "scripts.baseline", "--check"]


def test_every_check_of_a_set_shares_its_own_environment_drops_virtual_env_and_gets_the_toolchain_env(
    repo: Path, tmp_path: Path, uv: Path
) -> None:
    project, selection = _built(repo, tmp_path, APP_CATALOG, "minimal")

    checks_for(APP_CATALOG)(project, selection)

    environment = str(tmp_path / "scratch" / ".venvs" / "minimal")
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

    assert [result.name for result in results] == ["install", "every module imports", "ruff", "mypy", "tests"]


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
    assert out.startswith(
        "\n=== minimal: accounts\n  lock: none\n  PASS  install      fine\n  PASS  every module imports fine\n"
    )


def test_the_command_exits_1_when_a_check_fails(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("FAKE_UV_FAIL", "pytest")
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)

    code, out, _ = _verify_cli("minimal")

    assert code == 1
    assert "  FAIL  tests        boom\n" in out
    assert f"scratch copies left in {scratch}" in out


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
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))

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


@pytest.mark.parametrize("case", list(EXTENDS))
def test_an_extending_catalog_is_refused_before_anything_is_built(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))
    extends, name = EXTENDS[case]
    text = (repo / "blueprint.toml").read_text()
    (repo / "blueprint.toml").write_text(
        text.replace('requires_fastro = ">=0.1.0.dev0"', f'requires_fastro = ">=0.1.0.dev0"\n{extends}')
    )

    assert _verify_cli() == (3, "", EXTENDING.format(name=name))


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
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))
    reason = replace(repo, "blueprint.toml", make)

    code, _, err = _verify_cli("nosuchset")

    assert code == 2
    if make == "back":
        assert "'nosuchset' isn't a verify set" in err
    else:
        message = f"error: {repo / 'blueprint.toml'} {reason}. fastro reads blueprint.toml only as a regular file inside"
        assert message in err


def test_a_blueprint_toml_fastro_isnt_allowed_to_resolve_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))
    locked = repo.parent / "locked"
    locked.mkdir()
    (locked / "blueprint.toml").write_bytes((repo / "blueprint.toml").read_bytes())
    (repo / "blueprint.toml").unlink()
    (repo / "blueprint.toml").symlink_to(locked / "blueprint.toml")
    locked.chmod(0)
    try:
        code, _, err = _verify_cli("minimal")
    finally:
        locked.chmod(0o755)

    assert code == 3
    assert f"error: can't read {repo / 'blueprint.toml'}: Permission denied." in err


def test_a_blueprint_toml_fastro_cant_tell_is_there_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    locked = repo.parent / "locked"
    locked.mkdir()
    monkeypatch.setattr("fastro.commands.blueprint.Path.cwd", lambda: locked)
    locked.chmod(0)
    try:
        code, _, err = _verify_cli("minimal")
    finally:
        locked.chmod(0o755)

    assert code == 3
    assert f"error: can't read {locked / 'blueprint.toml'}: Permission denied." in err


def test_a_submodule_refuses_the_build_before_anything_is_written(repo: Path, tmp_path: Path) -> None:
    (repo / "backend/vendor").mkdir()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},backend/vendor")

    with pytest.raises(ApplyError, match="'backend/vendor' is a directory in the source, so nothing was changed"):
        build(repo, CATALOG, (), tmp_path / "core")

    assert not (tmp_path / "core").exists()


def test_a_refused_build_exits_1_saying_why_and_leaves_its_scratch_copy(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    health = repo / "backend/src/health.py"
    (repo / "backend/src/health_real.py").write_bytes(health.read_bytes())
    health.unlink()
    health.symlink_to("health_real.py")

    code, out, err = _verify_cli("minimal")

    assert code == 1
    assert "error: verify set 'minimal' can't be built: 'backend/src/health.py' is a symlink in the source" in err
    assert f"scratch copies left in {scratch}" in out


def test_a_set_whose_wiring_cant_render_exits_1_naming_the_set(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    toml = repo / "blueprint.toml"
    toml.write_text(
        toml.read_text().replace('fallback = "send_nowhere"\nfallback_imports = ["from ..email import send_nowhere"]\n', "")
    )

    code, out, err = _verify_cli("queues")

    assert code == 1
    assert 'error: verify set \'queues\' can\'t be built: wiring["backend/src/wiring/app.py"].slots["EMAIL_SENDER"]' in err
    assert "no contribution" in err
    assert "fastro blueprint lint" not in err
    assert f"scratch copies left in {scratch}" in out


def test_a_check_whose_program_cant_be_run_fails_saying_why(repo: Path, tmp_path: Path, uv: Path) -> None:
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o644)
    text = (MINI / "blueprint.toml").read_text().replace('run = ["python", "-c", "pass"]\nif = "tasks"', f'run = ["{script}"]')
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    project, selection = _built(repo, tmp_path, catalog, "minimal")

    smoke = checks_for(catalog)(project, selection)[-1]

    assert (smoke.name, smoke.ok) == ("smoke", False)
    assert smoke.last_line == f"{script} can't be run: Permission denied."


def test_a_directory_git_doesnt_track_exits_2_before_any_scratch_directory_is_made(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    shutil.rmtree(repo / ".git")
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))

    code, out, err = _verify_cli("minimal")

    assert (code, out) == (2, "")
    assert "isn't a git repository" in err


def test_an_unknown_set_exits_2_even_without_uv(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(repo)
    git = shutil.which("git")
    assert git is not None
    bin_dir = tmp_path / "only-git"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(bin_dir))

    code, _, err = _verify_cli("nope")

    assert code == 2
    assert "'nope' isn't a verify set" in err


def test_a_run_that_cant_finish_exits_3_even_after_a_failed_check(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    monkeypatch.setenv("FAKE_UV_FAIL", "pytest")
    built = verify_module.build

    def second_fails(
        root: Path,
        catalog: Blueprint,
        features: Sequence[str],
        into: Path,
        tracked: Sequence[RepoPath] | None = None,
        options: Mapping[tuple[str, str], str] | None = None,
    ) -> Path:
        if into.name == "full":
            raise PermissionError(13, "Permission denied", str(into))
        return built(root, catalog, features, into, tracked, options)

    monkeypatch.setattr("fastro.verify.build", second_fails)

    code, out, err = _verify_cli("minimal", "full")

    assert code == 3
    assert "  FAIL  tests        boom\n" in out
    assert f"scratch copies left in {scratch}" in out
    assert "Permission denied" in err


def test_a_tracked_file_fastro_isnt_allowed_to_resolve_fails_the_build_with_exit_3(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    locked = repo / "backend/src/locked"
    locked.mkdir()
    (locked / "secret.py").write_text("")
    _git(repo, "add", "backend/src/locked/secret.py")
    locked.chmod(0)
    try:
        code, out, err = _verify_cli("minimal")
    finally:
        locked.chmod(0o755)

    assert code == 3
    assert f"error: {locked / 'secret.py'}: Permission denied. Fix that, then run" in err
    assert f"scratch copies left in {scratch}" in out


def test_a_build_copies_from_the_tracked_files_it_is_given(repo: Path, tmp_path: Path) -> None:
    features = verify_sets(CATALOG)["minimal"]
    given = tuple(path for path in tracked_files(repo) if path.text != "backend/src/health.py")

    project = build(repo, CATALOG, features, tmp_path / "scratch" / "minimal", given)

    assert not (project / "backend/src/health.py").exists()
    assert (project / "backend/src/wiring/app.py").is_file()


def test_a_check_whose_cwd_cant_be_entered_fails_naming_the_cwd(repo: Path, tmp_path: Path, uv: Path) -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace('run = ["python", "-c", "pass"]\nif = "tasks"', 'run = ["python", "-c", "pass"]\ncwd = "sealed"')
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    project, selection = _built(repo, tmp_path, catalog, "minimal")
    (project / "sealed").mkdir()
    (project / "sealed").chmod(0)
    try:
        smoke = checks_for(catalog)(project, selection)[-1]
    finally:
        (project / "sealed").chmod(0o755)

    assert (smoke.name, smoke.ok) == ("smoke", False)
    assert smoke.last_line == f"Its cwd, {project / 'sealed'}, can't be entered."


def test_a_check_whose_cwd_is_under_a_directory_it_cant_search_says_it_cant_be_entered(
    repo: Path, tmp_path: Path, uv: Path
) -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace('run = ["python", "-c", "pass"]\nif = "tasks"', 'run = ["python", "-c", "pass"]\ncwd = "sealed/inner"')
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    project, selection = _built(repo, tmp_path, catalog, "minimal")
    (project / "sealed" / "inner").mkdir(parents=True)
    (project / "sealed").chmod(0)
    try:
        smoke = checks_for(catalog)(project, selection)[-1]
    finally:
        (project / "sealed").chmod(0o755)

    assert (smoke.name, smoke.ok) == ("smoke", False)
    assert smoke.last_line == f"Its cwd, {project / 'sealed' / 'inner'}, can't be entered."


def test_a_python_root_it_cant_search_fails_every_built_in_check_and_none_reads_as_absent(
    repo: Path, tmp_path: Path, uv: Path
) -> None:
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")
    backend = (project / "backend").resolve()
    backend.chmod(0)
    try:
        results = {result.name: result for result in checks_for(CATALOG)(project, selection)}
    finally:
        backend.chmod(0o755)

    assert {"install", "every module imports", "ruff", "mypy", "tests"} <= set(results)
    assert not any(result.ok for result in results.values())
    assert all("can't be entered" in result.last_line for result in results.values())
    for name, directory in [("ruff", "src"), ("mypy", "src"), ("tests", "tests")]:
        refused = f"{backend / directory} can't be entered: Permission denied."
        assert results[name] == Result(name, False, refused, refused)


def test_verify_lists_the_tracked_files_once_for_every_set(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _scratch_at(monkeypatch, tmp_path / "scratch")
    calls: list[Path] = []

    def counted(root: Path) -> tuple[RepoPath, ...]:
        calls.append(root)
        return tracked_files(root)

    monkeypatch.setattr("fastro.verify.tracked_files", counted)
    _, run = _checks(set())

    verify(repo, CATALOG, ["queues", "minimal"], run, keep=False)  # type: ignore[arg-type]

    assert calls == [repo]


@pytest.mark.parametrize(
    ("dev", "flags"),
    [
        ('[project.optional-dependencies]\ndev = ["ruff"]\n', ["--extra", "dev"]),
        ('[dependency-groups]\ndev = ["ruff"]\n', ["--group", "dev"]),
        (
            '[project.optional-dependencies]\ndev = ["ruff"]\n\n[dependency-groups]\ndev = ["mypy"]\n',
            ["--extra", "dev", "--group", "dev"],
        ),
        ('[project.optional-dependencies]\ndocs = ["mkdocs"]\n', []),
    ],
    ids=["extra", "group", "both", "neither"],
)
def test_install_comes_first_with_the_dev_dependencies_wherever_the_blueprint_keeps_them(
    repo: Path, tmp_path: Path, uv: Path, dev: str, flags: list[str]
) -> None:
    pyproject = repo / "backend/pyproject.toml"
    pyproject.write_text(pyproject.read_text() + "\n" + dev)
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")

    results = checks_for(CATALOG)(project, selection)

    backend = str((project / "backend").resolve())
    install, *runs = _calls(uv)
    assert results[0].name == "install"
    assert install["argv"] == ["sync", "--project", backend, *flags]
    assert all(
        call["argv"][: 3 + len(flags)] == ["run", "--project", backend, *flags] for call in runs if call["argv"][0] == "run"
    )
    assert all("--exact" not in call["argv"] for call in _calls(uv))


def test_a_failed_install_is_a_failed_check_and_the_other_checks_still_run(
    repo: Path, tmp_path: Path, uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_UV_FAIL", "sync")
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")

    results = checks_for(CATALOG)(project, selection)

    assert (results[0].name, results[0].ok, results[0].last_line) == ("install", False, "boom")
    assert [result.ok for result in results[1:4]] == [True, True, True]


@pytest.mark.parametrize("broken", ["not utf-8", "not toml", "project not a table", "missing"])
def test_a_build_pyproject_fastro_cant_read_installs_without_dev_dependencies(
    repo: Path, tmp_path: Path, uv: Path, broken: str
) -> None:
    project, selection = _built(repo, tmp_path, CATALOG, "minimal")
    pyproject = project / "backend/pyproject.toml"
    if broken == "not utf-8":
        pyproject.write_bytes(b"[project]\nname = '\xff'\n")
    elif broken == "not toml":
        pyproject.write_text("[project\n")
    elif broken == "project not a table":
        pyproject.write_text('project = "x"\n')
    else:
        pyproject.unlink()

    results = checks_for(CATALOG)(project, selection)

    backend = str((project / "backend").resolve())
    install, *runs = _calls(uv)
    assert install["argv"] == ["sync", "--project", backend]
    assert all(call["argv"][:4] == ["run", "--project", backend, "python"] for call in runs)
    assert [result.name for result in results][:2] == ["install", "every module imports"]


def test_the_migration_baseline_keeps_2_9_s_argv_without_the_dev_dependencies(repo: Path, tmp_path: Path, uv: Path) -> None:
    pyproject = repo / "backend/pyproject.toml"
    pyproject.write_text(pyproject.read_text() + '\n[project.optional-dependencies]\ndev = ["ruff"]\n')
    project, selection = _built(repo, tmp_path, APP_CATALOG, "minimal")

    checks_for(APP_CATALOG)(project, selection)

    backend = str((project / "backend").resolve())
    migrations = next(call for call in _calls(uv) if "scripts.baseline" in call["argv"])
    assert migrations["argv"] == ["run", "--project", backend, "python", "-m", "scripts.baseline", "--check"]


def test_a_render_error_makes_a_build_unbuildable(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*arguments: object) -> object:
        raise RenderError("SITE has no contribution and no fallback.")

    monkeypatch.setattr("fastro.verify.render", broken)

    with pytest.raises(Unbuildable, match="SITE has no contribution and no fallback."):
        build(repo, CATALOG, verify_sets(CATALOG)["minimal"], tmp_path / "scratch" / "minimal")


def test_any_other_value_error_in_a_build_is_a_bug_and_isnt_caught(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bug(*arguments: object) -> object:
        raise ValueError("a fastro bug")

    monkeypatch.setattr("fastro.verify.render", bug)

    with pytest.raises(ValueError, match="a fastro bug") as raised:
        build(repo, CATALOG, verify_sets(CATALOG)["minimal"], tmp_path / "scratch" / "minimal")
    assert type(raised.value) is ValueError


PYPROJECT = "backend/pyproject.toml"
NAME_RULE = "use letters, digits, '.', '_' and '-', starting and ending with a letter or digit, as PEP 508 says."
NAME_FIX = f'Set name under [project] in {PYPROJECT} to a valid PEP 508 name, such as name = "my-api".'
NAMELESS = [
    (
        '[project]\nversion = "0.1.0"\n',
        f"{PYPROJECT} has no [project].name string: {NAME_RULE} {NAME_FIX}",
    ),
    (
        '[project]\nname = 7\nversion = "0.1.0"\n',
        f"{PYPROJECT} has no [project].name string: {NAME_RULE} {NAME_FIX}",
    ),
    (
        '[project]\nname = "mini backend"\nversion = "0.1.0"\n',
        f"{PYPROJECT} has [project].name 'mini backend', which isn't a valid name: {NAME_RULE} {NAME_FIX}",
    ),
    (b'[project]\nname = "mini\xff"\n', f"{PYPROJECT} isn't UTF-8 text. Save {PYPROJECT} as UTF-8."),
    ("[project\n", f"{PYPROJECT} isn't valid TOML: "),
    (None, f"{PYPROJECT} isn't a tracked file. Add {PYPROJECT} to git."),
]


@pytest.mark.parametrize(
    ("text", "said"), NAMELESS, ids=["missing", "not-text", "invalid", "not-utf-8", "not-toml", "untracked"]
)
def test_a_pyproject_a_build_cant_take_its_name_from_exits_1_before_anything_is_built(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, text: str | bytes | None, said: str
) -> None:
    monkeypatch.chdir(repo)
    file = repo / PYPROJECT
    if text is None:
        _git(repo, "rm", "-q", "--cached", PYPROJECT)
    elif isinstance(text, bytes):
        file.write_bytes(text)
    else:
        file.write_text(text)
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))

    code, out, err = _verify_cli("minimal")

    assert (code, out) == (1, "")
    assert err.startswith(f"error: verify can't build a set: {said}")
    assert _calls(uv) == []


@pytest.mark.usefixtures("hang_guard")
def test_a_pyproject_fastro_may_not_read_exits_1_saying_why(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, replace: Callable[[Path, str, str], str]
) -> None:
    monkeypatch.chdir(repo)
    reason = replace(repo, PYPROJECT, "outside")
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))

    code, _, err = _verify_cli("minimal")

    assert code == 1
    assert err == (
        f"error: verify can't build a set: {PYPROJECT} {reason}, and verify reads only regular files inside the repo. "
        f"Make {PYPROJECT} a regular file in the repo.\n"
    )


def test_a_pyproject_the_system_wont_let_verify_open_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    (repo / PYPROJECT).chmod(0)
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))
    try:
        code, _, err = _verify_cli("minimal")
    finally:
        (repo / PYPROJECT).chmod(0o644)

    assert code == 3
    assert f"error: {repo / PYPROJECT}: Permission denied." in err


ENV_EXAMPLE = "backend/.env.example"
NOT_UTF_8 = "# caf\xe9\nA=1\n".encode("latin-1")


def test_a_tracked_env_example_that_isnt_utf_8_exits_1_before_anything_is_built(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    (repo / ENV_EXAMPLE).write_bytes(NOT_UTF_8)
    _git(repo, "add", ENV_EXAMPLE)
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))

    code, out, err = _verify_cli("minimal")

    assert (code, out) == (1, "")
    assert err == f"error: verify can't build a set: {ENV_EXAMPLE} isn't UTF-8 text. Save {ENV_EXAMPLE} as UTF-8.\n"
    assert _calls(uv) == []


@pytest.mark.parametrize(
    ("text", "tracked"), [(NOT_UTF_8, False), ("\ufeffA=1\n".encode(), True)], ids=["untracked", "byte-order-mark"]
)
def test_an_env_example_a_build_can_read_or_wont_copy_isnt_refused(repo: Path, text: bytes, tracked: bool) -> None:
    (repo / ENV_EXAMPLE).write_bytes(text)
    if tracked:
        _git(repo, "add", ENV_EXAMPLE)

    assert verify(repo, CATALOG, ["minimal"], lambda project, selection: [], keep=False) == 0


@pytest.mark.usefixtures("hang_guard")
def test_an_env_example_fastro_may_not_read_isnt_read_for_utf_8(
    repo: Path,
    uv: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace: Callable[[Path, str, str], str],
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(repo)
    (repo / ENV_EXAMPLE).write_bytes(NOT_UTF_8)
    _git(repo, "add", ENV_EXAMPLE)
    replace(repo, ENV_EXAMPLE, "outside")
    _scratch_at(monkeypatch, tmp_path / "scratch")

    code, _, err = _verify_cli("minimal")

    assert (code, err) == (
        1,
        f"error: verify set 'minimal' can't be built: '{ENV_EXAMPLE}' is a symlink in the source, and apply doesn't "
        "follow one, so nothing was changed.\n",
    )


def test_an_env_example_the_system_wont_let_verify_open_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    (repo / ENV_EXAMPLE).write_text("A=1\n")
    _git(repo, "add", ENV_EXAMPLE)
    (repo / ENV_EXAMPLE).chmod(0)
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))
    try:
        code, _, err = _verify_cli("minimal")
    finally:
        (repo / ENV_EXAMPLE).chmod(0o644)

    assert code == 3
    assert f"error: {repo / ENV_EXAMPLE}: Permission denied." in err


def test_an_env_example_verify_isnt_allowed_to_resolve_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    locked = repo / "locked"
    locked.mkdir()
    (locked / "env").write_text("A=1\n")
    (repo / ENV_EXAMPLE).symlink_to(locked / "env")
    _git(repo, "add", ENV_EXAMPLE, "locked/env")
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))
    locked.chmod(0o600)
    try:
        code, out, err = _verify_cli("minimal")
    finally:
        locked.chmod(0o755)

    assert (code, out) == (3, "")
    assert "Permission denied" in err
    assert _calls(uv) == []


def test_verify_refuses_a_nameless_pyproject_itself(repo: Path) -> None:
    (repo / PYPROJECT).write_text('[project]\nversion = "0.1.0"\n')

    with pytest.raises(Unbuildable, match="has no \\[project\\].name string"):
        verify(repo, CATALOG, ["minimal"], lambda project, selection: [], keep=False)


def test_a_pyproject_verify_isnt_allowed_to_resolve_exits_3(repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))
    (repo / "backend").chmod(0o600)
    try:
        code, out, err = _verify_cli("minimal")
    finally:
        (repo / "backend").chmod(0o755)

    assert (code, out) == (3, "")
    assert f"error: {repo / PYPROJECT}: Permission denied. Fix that, then run fastro blueprint verify again.\n" in err
    assert _calls(uv) == []


def _runtime(pyproject: Path) -> object:
    return tomllib.loads(pyproject.read_text())["project"]["dependencies"]


def test_a_set_with_a_feature_s_dependency_and_one_without_both_pass_each_with_its_own_dependencies_and_environment(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    toml = repo / "blueprint.toml"
    text = toml.read_text().replace('[[verify.checks]]\nname = "smoke"\nrun = ["python", "-c", "pass"]\nif = "tasks"\n', "")
    toml.write_text(
        text.replace(
            'summary = "A cache for slow reads"\n', 'summary = "A cache for slow reads"\ndependencies = ["cachetools>=5"]\n'
        )
    )

    code, out, _ = _verify_cli("queues", "minimal", "--keep")

    assert code == 0
    assert "FAIL" not in out
    assert _runtime(scratch / "queues/backend/pyproject.toml") == ["cachetools>=5"]
    assert _runtime(scratch / "minimal/backend/pyproject.toml") == []
    installs = {call["cwd"]: call["env"]["UV_PROJECT_ENVIRONMENT"] for call in _calls(uv) if call["argv"][0] == "sync"}
    assert installs == {
        str((scratch / name / "backend").resolve()): str(scratch / ".venvs" / name) for name in ("queues", "minimal")
    }


def test_a_set_whose_dependencies_cant_be_merged_exits_1_naming_the_set(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    toml = repo / "blueprint.toml"
    text = toml.read_text().replace("[presets]\n", '[blueprint.dependencies]\nruntime = ["uvloop"]\n\n[presets]\n')
    toml.write_text(
        text.replace(
            'summary = "A cache for slow reads"\n',
            """summary = "A cache for slow reads"\ndependencies = ["uvloop; os_name == 'posix'"]\n""",
        )
    )

    code, out, err = _verify_cli("queues")

    assert code == 1
    assert "error: verify set 'queues' can't be built: uvloop has entries with different environment markers" in err
    assert f"scratch copies left in {scratch}" in out


CACHE_OPTION = 'choices = ["memory", "redis"]\ndefault = "memory"\n'
CACHE_ENV = CACHE_OPTION + 'env = { CACHE_BACKEND = "backend-{value}" }\n'
EXAMPLE = "APP=1\n\n# ===\n# Cache\n# ===\n# @fastro if=cache\n# @fastro option=cache.backend\r\nCACHE_BACKEND=memory\r\n"


def _env_repo(repo: Path, example: str) -> None:
    toml = repo / "blueprint.toml"
    toml.write_text(toml.read_text().replace(CACHE_OPTION, CACHE_ENV))
    (repo / ENV_EXAMPLE).write_text(example)
    _git(repo, "add", ENV_EXAMPLE)


def test_each_build_gets_its_env_example_processed_for_its_set(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    scratch = tmp_path / "scratch"
    _scratch_at(monkeypatch, scratch)
    _env_repo(repo, EXAMPLE)

    _verify_cli("queues", "minimal", "--keep")

    assert (
        scratch / "queues" / ENV_EXAMPLE
    ).read_bytes() == b"APP=1\n\n# ===\n# Cache\n# ===\nCACHE_BACKEND=backend-memory\r\n"
    assert (scratch / "minimal" / ENV_EXAMPLE).read_bytes() == b"APP=1\n\n"
    assert (repo / ENV_EXAMPLE).read_bytes() == EXAMPLE.encode()


@pytest.mark.parametrize(
    ("example", "said"),
    [
        (EXAMPLE + "# @fastro option=cache.nope\nX=1\n", "line 9: 'cache.nope' isn't an option of this catalog."),
        ("APP=1\n", "CACHE_BACKEND has no '# @fastro option=cache.backend' marker directly above a CACHE_BACKEND= line."),
        (
            EXAMPLE + "# @fastro option=cache.backend\nCACHE_BACKEND=x\n",
            "line 9: CACHE_BACKEND has 2 '# @fastro option=cache.backend' markers, on lines 7 and 9, and needs one.",
        ),
    ],
    ids=["marker-line", "unmarked-var", "marked-twice"],
)
def test_an_env_example_6_1_refuses_exits_1_before_anything_is_built_saying_why(
    repo: Path, uv: Path, monkeypatch: pytest.MonkeyPatch, example: str, said: str
) -> None:
    monkeypatch.chdir(repo)
    _env_repo(repo, example)
    monkeypatch.setattr("fastro.verify.mkdtemp", lambda prefix: pytest.fail("built"))

    code, out, err = _verify_cli("minimal")

    assert (code, out) == (1, "")
    assert err == f"error: verify can't build a set: {ENV_EXAMPLE} {said} Run fastro blueprint lint to see every problem.\n"
    assert _calls(uv) == []


def test_a_build_of_an_env_example_6_1_refuses_is_unbuildable(repo: Path, tmp_path: Path) -> None:
    _env_repo(repo, "APP=1\n")
    catalog = blueprint.load("blueprint.toml", (repo / "blueprint.toml").read_text(), Version("0.1.0"))

    with pytest.raises(Unbuildable, match="^CACHE_BACKEND has no '# @fastro option=cache.backend' marker"):
        build(repo, catalog, (), tmp_path / "core")
