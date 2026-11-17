"""Listing a repo's tracked files, and applying a plan's changes into a directory (A4 item 2)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.apply import ApplyError, Change, RepositoryError, apply, changes, tracked_files, unreadable
from fastro.engine import blueprint, lock
from fastro.engine.plan import plan
from fastro.engine.render import render
from fastro.engine.resolve import select
from fastro.engine.spec import RepoPath
from fastro.tools import MissingToolError

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
CATALOG = blueprint.load("blueprint.toml", (MINI / "blueprint.toml").read_text(), Version("0.1.0"))
FILES = tuple(RepoPath(str(path.relative_to(MINI))) for path in sorted(MINI.rglob("*")) if path.is_file())


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    _git(root, "add", "-A")
    return root


def test_tracked_files_are_what_git_tracks_sorted(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", {"b.py": "", "a/x.py": "", "a/y z.txt": ""})
    (repo / "untracked.py").write_text("")

    assert tracked_files(repo) == (RepoPath("a/x.py"), RepoPath("a/y z.txt"), RepoPath("b.py"))


def test_a_tracked_file_deleted_from_the_working_tree_isnt_listed(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", {"kept.py": "", "gone.py": ""})
    (repo / "gone.py").unlink()

    assert tracked_files(repo) == (RepoPath("kept.py"),)


def test_a_directory_that_isnt_a_git_repo_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="isn't a git repository"):
        tracked_files(tmp_path)


def test_without_git_on_path_listing_tracked_files_raises_missing_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(MissingToolError) as caught:
        tracked_files(tmp_path)

    assert caught.value.tool == "git"


def test_a_plan_becomes_a_copy_per_copy_op_and_a_write_per_render_op() -> None:
    made = plan(CATALOG, select(CATALOG, ("accounts",)), FILES)
    rendered = render(made)

    found = changes(made, rendered)

    assert [change.path for change in found if change.kind == "copy"] == [op.path for op in made.ops if op.op == "copy"]
    assert {change.path: change.text for change in found if change.kind == "write"} == rendered
    assert [change for change in found if change.kind == "delete"] == []


def test_a_plan_s_only_deletes_are_its_delete_ops() -> None:
    rows = "".join(
        f'"{path}" = {{ sha256 = "{"0" * 64}", origin = "{origin}" }}\n'
        for path, origin in (("backend/src/modules/billing/routes.py", "feature:billing"), ("backend/src/mount.py", "core"))
    )
    previous = lock.load(
        "lock.toml",
        '[lock]\nschema = 1\nfastro = "0.1.0"\nblueprint = { name = "mini", version = "0.1.0", source = "../mini" }\n'
        f"features = []\n[files]\n{rows}",
    )
    made = plan(CATALOG, select(CATALOG, ("accounts",)), FILES, previous=previous)

    found = changes(made, render(made))

    assert [change.path for change in found if change.kind == "delete"] == [op.path for op in made.ops if op.op == "delete"]
    assert [op.path for op in made.ops if op.op == "delete"] == ["backend/src/modules/billing/routes.py"]


def test_apply_copies_writes_and_deletes_and_keeps_a_copied_file_s_mode(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts" / "run.sh").write_text("echo hi\n")
    (source / "scripts" / "run.sh").chmod(0o755)
    (target / "old").mkdir(parents=True)
    (target / "old" / "gone.py").write_text("")
    (target / "kept.py").write_text("kept\n")

    apply(
        [Change("copy", "scripts/run.sh"), Change("write", "src/wiring/app.py", "x = 1\n"), Change("delete", "old/gone.py")],
        source,
        target,
    )

    assert (target / "scripts" / "run.sh").read_text() == "echo hi\n"
    assert os.access(target / "scripts" / "run.sh", os.X_OK)
    assert (target / "src" / "wiring" / "app.py").read_text() == "x = 1\n"
    assert not (target / "old" / "gone.py").exists()
    assert (target / "kept.py").read_text() == "kept\n"


def test_deleting_a_file_thats_already_absent_is_fine(tmp_path: Path) -> None:
    apply([Change("delete", "absent.py")], tmp_path / "source", tmp_path / "target")


@pytest.mark.parametrize("path", ["../outside.py", "/etc/outside.py", "a/../../outside.py"])
def test_a_path_outside_the_target_is_refused_and_nothing_is_written(tmp_path: Path, path: str) -> None:
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ApplyError):
        apply([Change("write", "first.py", ""), Change("write", path, "x")], tmp_path, target)

    assert list(target.iterdir()) == []
    assert not (tmp_path / "outside.py").exists()


def test_a_symlinked_directory_in_the_target_isnt_followed(tmp_path: Path) -> None:
    target, elsewhere = tmp_path / "target", tmp_path / "elsewhere"
    target.mkdir()
    elsewhere.mkdir()
    (target / "src").symlink_to(elsewhere)

    with pytest.raises(ApplyError, match="'src' is a symlink"):
        apply([Change("write", "src/app.py", "x = 1\n")], tmp_path, target)

    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("kind", ["write", "delete"])
def test_a_symlinked_file_in_the_target_is_neither_written_through_nor_deleted(tmp_path: Path, kind: str) -> None:
    target, elsewhere = tmp_path / "target", tmp_path / "elsewhere.py"
    target.mkdir()
    elsewhere.write_text("mine\n")
    (target / "app.py").symlink_to(elsewhere)

    with pytest.raises(ApplyError, match="'app.py' is a symlink"):
        apply([Change(kind, "app.py", "x = 1\n")], tmp_path, target)

    assert elsewhere.read_text() == "mine\n"
    assert (target / "app.py").is_symlink()


def test_a_symlinked_source_file_isnt_copied(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (tmp_path / "secret.txt").write_text("secret\n")
    (source / "link.txt").symlink_to(tmp_path / "secret.txt")

    with pytest.raises(ApplyError, match="'link.txt' is a symlink in the source"):
        apply([Change("copy", "link.txt")], source, target)

    assert not (target / "link.txt").exists()


def test_only_limits_apply_to_the_paths_it_names_and_nothing_is_changed_otherwise(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "README.md").write_text("mine\n")
    only = {"backend/src/wiring/app.py"}

    with pytest.raises(ApplyError, match="'README.md' isn't a file this step may change"):
        apply([Change("write", "backend/src/wiring/app.py", "x\n"), Change("delete", "README.md")], tmp_path, target, only=only)

    assert (target / "README.md").read_text() == "mine\n"
    assert not (target / "backend").exists()

    apply([Change("write", "backend/src/wiring/app.py", "x\n")], tmp_path, target, only=only)

    assert (target / "backend" / "src" / "wiring" / "app.py").read_text() == "x\n"


def test_mini_s_full_plan_applied_into_an_empty_directory_gives_mini_s_tracked_wiring(tmp_path: Path) -> None:
    made = plan(CATALOG, select(CATALOG, CATALOG.presets["full"]), FILES)

    apply(changes(made, render(made)), MINI, tmp_path)

    for file in CATALOG.wiring:
        assert (tmp_path / file.path.text).read_text() == (MINI / file.path.text).read_text()


TEXT = 'export const MENU = [{ "label": "Café → Menu", "feature": "core" }] as const;\nx = 1\n'


def test_a_write_is_utf_8_with_its_newlines_as_given(tmp_path: Path) -> None:
    apply([Change("write", "frontend/menu.ts", TEXT)], tmp_path, tmp_path / "target")

    assert (tmp_path / "target" / "frontend" / "menu.ts").read_bytes() == TEXT.encode("utf-8")


def test_a_write_gives_the_same_bytes_under_a_non_utf_8_locale(tmp_path: Path) -> None:
    script = (
        "import sys\nfrom pathlib import Path\nfrom fastro.apply import Change, apply\n"
        "apply([Change('write', 'menu.ts', sys.argv[1])], Path(sys.argv[2]), Path(sys.argv[2]) / 'target')\n"
    )
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    run = subprocess.run([sys.executable, "-c", script, TEXT, str(tmp_path)], env=env, capture_output=True, text=True)

    assert run.returncode == 0, run.stderr
    assert (tmp_path / "target" / "menu.ts").read_bytes() == TEXT.encode("utf-8")


@pytest.fixture
def inside(tmp_path: Path) -> Path:
    """A repo with one regular file, ``real.py``, beside a directory outside it holding ``secret.py``."""
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.py").write_text("x = 1\n")
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "real.py").write_text("x = 1\n")
    return repo


def test_a_regular_file_inside_the_repo_is_readable(inside: Path) -> None:
    assert unreadable(inside, "real.py") is None


def test_a_symlink_to_a_regular_file_inside_the_repo_is_readable(inside: Path) -> None:
    (inside / "pkg" / "link.py").symlink_to("../real.py")

    assert unreadable(inside, "pkg/link.py") is None


def test_a_symlink_that_leaves_the_repo_and_comes_back_is_readable(inside: Path) -> None:
    (inside.parent / "outside" / "hop.py").symlink_to(inside / "real.py")
    (inside / "back.py").symlink_to(inside.parent / "outside" / "hop.py")

    assert unreadable(inside, "back.py") is None


def test_a_symlink_that_leads_outside_the_repo_isnt_readable(inside: Path) -> None:
    (inside / "out.py").symlink_to(inside.parent / "outside" / "secret.py")

    assert unreadable(inside, "out.py") == "leads outside the repo"


def test_a_directory_whose_symlink_leads_outside_the_repo_isnt_readable(inside: Path) -> None:
    (inside / "away").symlink_to(inside.parent / "outside")

    assert unreadable(inside, "away/secret.py") == "leads outside the repo"


@pytest.mark.parametrize("make", ["fifo", "directory"])
def test_what_isnt_a_regular_file_isnt_readable(inside: Path, make: str) -> None:
    target = inside / "thing"
    if make == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()

    assert unreadable(inside, "thing") == "isn't a regular file"


def test_a_symlink_to_a_device_leads_outside_the_repo(inside: Path) -> None:
    (inside / "zero").symlink_to("/dev/zero")

    assert unreadable(inside, "zero") == "leads outside the repo"


def test_a_symlink_to_nothing_isnt_readable(inside: Path) -> None:
    (inside / "gone.py").symlink_to("missing.py")

    assert unreadable(inside, "gone.py") == "leads to nothing"


def test_a_symlink_loop_isnt_readable(inside: Path) -> None:
    (inside / "a.py").symlink_to("b.py")
    (inside / "b.py").symlink_to("a.py")

    assert unreadable(inside, "a.py") == "has symlinks that can't be followed"


def test_a_repo_reached_through_a_symlink_is_compared_resolved(inside: Path) -> None:
    alias = inside.parent / "alias"
    alias.symlink_to(inside)

    assert unreadable(alias, "real.py") is None


def test_a_symlink_through_a_file_leads_to_nothing(inside: Path) -> None:
    (inside / "through.py").symlink_to("real.py/x")

    assert unreadable(inside, "through.py") == "leads to nothing"


def test_a_file_fastro_cant_resolve_says_so_without_blaming_a_symlink(inside: Path) -> None:
    locked = inside / "locked"
    locked.mkdir()
    (locked / "f.py").write_text("")
    (inside / "in.py").symlink_to("locked/f.py")
    locked.chmod(0)
    try:
        assert unreadable(inside, "in.py") == "can't be resolved: Permission denied"
    finally:
        locked.chmod(0o755)
