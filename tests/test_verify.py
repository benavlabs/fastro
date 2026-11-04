"""``fastro blueprint verify``'s harness: which sets, each built through the engine, and the drill's report (A4 8a)."""

import shutil
import subprocess
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.resolve import Selection
from fastro.verify import REPORTED_LINES, Result, UnknownSet, build, report, verify, verify_sets

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
