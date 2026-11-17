"""``fastro sync`` in a ``source = true`` repo: the wiring only, written, deleted or diffed (spec 3, 8; rev 1.30)."""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fastro import sync as syncing
from fastro.apply import Change
from fastro.cli import app

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
MENU = "frontend/menu.ts"
SITE = "backend/src/wiring/admin.py"
APP = "backend/src/wiring/app.py"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mini"
    shutil.copytree(MINI, root)
    monkeypatch.chdir(root)
    return root


def _sync(*arguments: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, ["sync", *arguments])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result.exit_code, result.stdout, result.stderr


def _tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _without_admin(root: Path) -> None:
    toml = root / "fastro.toml"
    toml.write_text(toml.read_text().replace(', "admin"]', "]"))


def test_a_repo_in_sync_needs_nothing(repo: Path) -> None:
    before = _tree(repo)

    assert _sync("--check") == (0, "", "")
    assert _sync("--dry-run") == (0, "", "")
    assert _sync() == (0, "", "")
    assert _tree(repo) == before


def test_check_prints_the_diff_of_an_edited_wiring_file_and_exits_1_without_writing(repo: Path) -> None:
    (repo / APP).write_text((repo / APP).read_text().replace('"/billing"', '"/bills"'))
    before = _tree(repo)

    code, out, _ = _sync("--check")

    assert code == 1
    assert out.startswith(f"--- a/{APP}\n+++ b/{APP}\n@@ ")
    assert '-    Mount(billing_router, "/bills"),\n+    Mount(billing_router, "/billing"),\n' in out
    assert _tree(repo) == before


def test_dry_run_prints_each_path_it_would_write_and_writes_nothing(repo: Path) -> None:
    (repo / APP).write_text("edited\n")
    (repo / "backend/src/wiring/health.py").unlink()
    before = _tree(repo)

    assert _sync("--dry-run") == (0, "backend/src/wiring/app.py\nbackend/src/wiring/health.py\n", "")
    assert _tree(repo) == before


def test_sync_rewrites_an_edited_or_missing_wiring_file_and_touches_nothing_else(repo: Path) -> None:
    committed = _tree(repo)
    (repo / APP).write_text("edited\n")
    (repo / "backend/src/wiring/health.py").unlink()
    (repo / "backend/src/app.py").write_text("my own edit\n")

    code, out, _ = _sync()

    assert (code, out) == (0, "wrote backend/src/wiring/app.py\nwrote backend/src/wiring/health.py\n")
    assert _tree(repo) == {**committed, "backend/src/app.py": b"my own edit\n"}


def test_a_missing_wiring_file_diffs_from_dev_null(repo: Path) -> None:
    (repo / "backend/src/wiring/health.py").unlink()

    code, out, _ = _sync("--check")

    assert code == 1
    assert out.startswith("--- /dev/null\n+++ b/backend/src/wiring/health.py\n")


def test_a_wiring_file_the_selection_no_longer_renders_is_deleted_and_diffs_to_dev_null(repo: Path) -> None:
    _without_admin(repo)

    code, out, _ = _sync("--check")

    assert code == 1
    assert f"--- a/{MENU}\n+++ /dev/null\n" in out
    assert "-from ..modules.admin.site import site as SITE\n+from ..admin_default import site as SITE\n" in out
    assert _sync("--dry-run")[1].splitlines() == [SITE, MENU]


def test_sync_deletes_the_wiring_files_the_selection_no_longer_renders_and_nothing_else(repo: Path) -> None:
    _without_admin(repo)
    before = _tree(repo)

    code, out, _ = _sync()

    assert code == 0
    assert out.splitlines() == [f"wrote {SITE}", f"deleted {MENU}"]
    after = _tree(repo)
    assert set(before) - set(after) == {MENU}
    assert {path for path in after if after[path] != before[path]} == {SITE}
    assert _sync("--check") == (0, "", "")


def test_a_file_that_isnt_utf_8_differs(repo: Path) -> None:
    (repo / APP).write_bytes(b"\xff\xfe")

    code, out, _ = _sync("--check")

    assert code == 1
    assert f"--- a/{APP}\n+++ b/{APP}\n" in out


def test_check_and_dry_run_together_are_a_usage_error(repo: Path) -> None:
    code, _, err = _sync("--check", "--dry-run")

    assert code == 2
    assert "--check and --dry-run" in err


def test_a_directory_without_fastro_toml_is_a_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    code, _, err = _sync("--check")

    assert code == 2
    assert "has no fastro.toml" in err


def test_a_project_s_sync_isnt_here_yet_and_says_so(repo: Path) -> None:
    toml = repo / "fastro.toml"
    toml.write_text(toml.read_text().replace("source = true\n", ""))

    code, _, err = _sync("--check")

    assert code == 2
    assert "doesn't set source = true" in err


def test_a_catalog_problem_is_reported_and_exits_1(repo: Path) -> None:
    catalog = repo / "blueprint.toml"
    catalog.write_text(catalog.read_text().replace("schema = 1", 'schema = "one"'))

    code, _, err = _sync("--check")

    assert code == 1
    assert "blueprint.toml:4: blueprint.schema: should be an integer, not a string." in err


def test_a_fastro_toml_problem_is_reported_and_exits_1(repo: Path) -> None:
    toml = repo / "fastro.toml"
    toml.write_text(toml.read_text().replace('"billing.provider" = "stripe"', '"billing.provider" = "square"'))

    code, _, err = _sync("--check")

    assert code == 1
    assert "fastro.toml:" in err and "square" in err


def test_a_selection_the_catalog_refuses_exits_1(repo: Path) -> None:
    toml = repo / "fastro.toml"
    toml.write_text(toml.read_text() + '\n[app]\ncontributes = { admin_site = ["src.app_site:site"] }\n')

    code, _, err = _sync("--check")

    assert code == 1
    assert "'admin_site' is a one point" in err


def test_the_repo_s_options_decide_what_its_wiring_renders(repo: Path) -> None:
    catalog = repo / "blueprint.toml"
    catalog.write_text(
        catalog.read_text().replace(
            'if = "cache.backend=redis"\nservices = ["redis"]\n',
            'if = "cache.backend=redis"\nservices = ["redis"]\ncontributes = { health_checks = ["src.health:redis"] }\n',
        )
    )
    assert _sync("--check")[0] == 0
    toml = repo / "fastro.toml"
    toml.write_text(toml.read_text().replace('"cache.backend" = "memory"', '"cache.backend" = "redis"'))

    code, out, _ = _sync("--check")

    assert code == 1
    assert "+from ..health import database, redis\n" in out


def test_changes_are_listed_by_path_not_in_catalog_order(repo: Path) -> None:
    (repo / APP).write_text("edited\n")
    (repo / SITE).write_text("edited\n")

    assert _sync("--dry-run")[1].splitlines() == [SITE, APP]


def test_sync_refuses_to_change_a_file_that_isnt_wiring_whatever_it_was_asked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def outside(source: syncing.Source) -> list[Change]:
        return [Change("write", "backend/src/app.py", "replaced\n")]

    monkeypatch.setattr(syncing, "wiring_changes", outside)
    before = _tree(repo)

    code, _, err = _sync()

    assert code == 1
    assert "'backend/src/app.py' isn't a file this step may change" in err
    assert _tree(repo) == before


def test_a_symlinked_wiring_file_is_refused_and_exits_1(repo: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("mine\n")
    (repo / APP).unlink()
    (repo / APP).symlink_to(elsewhere)

    code, _, err = _sync()

    assert code == 1
    assert "is a symlink in the target" in err
    assert elsewhere.read_text() == "mine\n"


def test_a_blueprint_repo_without_its_blueprint_toml_is_a_usage_error(repo: Path) -> None:
    (repo / "blueprint.toml").unlink()

    code, _, err = _sync("--check")

    assert code == 2
    assert "fastro.toml sets source = true, but there's no blueprint.toml beside it." in err


@pytest.mark.parametrize("name", ["fastro.toml", "blueprint.toml"])
def test_a_file_that_cant_be_read_is_reported_with_its_path_and_exits_3(
    repo: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    read = Path.read_bytes

    def refuse(path: Path) -> bytes:
        if path.name == name:
            raise PermissionError(13, "Permission denied", str(path))
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", refuse)

    code, _, err = _sync("--check")

    assert code == 3
    assert f"error: can't read {repo / name}: Permission denied." in err


@pytest.mark.parametrize("name", ["fastro.toml", "blueprint.toml"])
def test_a_file_that_isnt_utf_8_is_a_problem_at_its_line(repo: Path, name: str) -> None:
    (repo / name).write_bytes((repo / name).read_bytes() + b"# \xff\n")

    code, _, err = _sync("--check")

    assert code == 1
    assert "isn't UTF-8 text." in err


def test_a_byte_order_mark_is_read_past(repo: Path) -> None:
    for name in ("fastro.toml", "blueprint.toml"):
        (repo / name).write_bytes(b"\xef\xbb\xbf" + (repo / name).read_bytes())

    assert _sync("--check") == (0, "", "")


Replace = Callable[[Path, str, str], str]


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", ["outside", "fifo", "dangling", "directory"])
@pytest.mark.parametrize("name", ["blueprint.toml", "fastro.toml"])
def test_a_config_file_fastro_may_not_read_exits_2_saying_why(repo: Path, replace: Replace, name: str, make: str) -> None:
    reason = replace(repo, name, make)

    code, out, err = _sync("--check")

    assert (code, out) == (2, "")
    assert f"error: {repo / name} {reason}. fastro reads {name} only as a regular file inside the repo." in err


@pytest.mark.parametrize("name", ["blueprint.toml", "fastro.toml"])
def test_a_config_file_whose_symlink_leaves_the_repo_and_comes_back_is_read(repo: Path, replace: Replace, name: str) -> None:
    replace(repo, name, "back")

    assert _sync("--check") == (0, "", "")


@pytest.mark.usefixtures("hang_guard")
def test_check_never_prints_a_wiring_file_whose_symlink_leads_outside_the_repo(repo: Path, replace: Replace) -> None:
    replace(repo, APP, "outside")
    (repo.parent / "outside" / APP).write_text("SECRET = 'outside the repo'\n")

    code, out, _ = _sync("--check")

    assert code == 1
    assert out.startswith(f"--- a/{APP}\n+++ b/{APP}\n")
    assert "outside the repo" not in out


def test_check_diffs_a_wiring_symlink_that_leads_to_nothing_from_dev_null(repo: Path, replace: Replace) -> None:
    replace(repo, APP, "dangling")

    code, out, _ = _sync("--check")

    assert code == 1
    assert out.startswith(f"--- /dev/null\n+++ b/{APP}\n@@ ")


@pytest.mark.parametrize("name", ["blueprint.toml", "fastro.toml"])
def test_a_config_file_fastro_isnt_allowed_to_resolve_exits_3(repo: Path, name: str) -> None:
    locked = repo.parent / "locked"
    locked.mkdir()
    (locked / name).write_bytes((repo / name).read_bytes())
    (repo / name).unlink()
    (repo / name).symlink_to(locked / name)
    locked.chmod(0)
    try:
        code, out, err = _sync("--check")
    finally:
        locked.chmod(0o755)

    assert (code, out) == (3, "")
    assert f"error: can't read {repo / name}: Permission denied." in err


def test_a_fastro_toml_fastro_cant_tell_is_there_is_unreadable(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(syncing.Unreadable, match=f"can't read {locked / 'fastro.toml'}: Permission denied"):
            syncing.load_source(locked)
    finally:
        locked.chmod(0o755)
