"""Listing a repo's tracked files, and applying a plan's changes into a directory (A4 item 2)."""

import io
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.apply import (
    ApplyError,
    Change,
    RepositoryError,
    apply,
    changes,
    check,
    export_subst,
    tracked_files,
)
from fastro.engine import blueprint, lock
from fastro.engine.files import file_kind, unreadable, unreadable_config
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


ATTRIBUTED = {
    ".gitattributes": "docs/ export-ignore\nz.toml export-ignore\nvalued.txt export-ignore=yes\nslashless export-ignore\n"
    "v.txt export-subst\nsubst/ export-subst\nboth.txt export-subst export-ignore\nsubst-valued.txt export-subst=yes\n",
    "a/.gitattributes": "x export-ignore\n",
    "docs/i.md": "",
    "docs/sub/j.md": "",
    "z.toml": "",
    "valued.txt": "",
    "slashless/k.md": "",
    "a/x": "",
    "a/y": "",
    "keep": "",
    "v.txt": "",
    "subst/w.txt": "",
    "both.txt": "",
    "subst-valued.txt": "",
}


def test_tracked_files_leave_out_what_export_ignore_marks_as_git_archive_does(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", ATTRIBUTED)
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x")
    archived = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.attributesFile=/dev/null", "archive", "HEAD"], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archived)) as tar:
        members = sorted(member.name for member in tar.getmembers() if member.isfile())

    listed = [path.text for path in tracked_files(repo)]

    assert listed == sorted(listed)
    assert sorted(listed) == members
    assert "valued.txt" in listed and "a/y" in listed
    assert not {"docs/i.md", "docs/sub/j.md", "z.toml", "slashless/k.md", "a/x", "both.txt"} & set(listed)


def test_only_the_repo_s_own_gitattributes_decide_what_export_ignore_marks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo", {"keep": "", "kept": "", "kept-too": ""})
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("keep export-ignore\n")
    (tmp_path / "attributes").write_text("kept export-ignore\n")
    (tmp_path / "gitconfig").write_text(f"[core]\n\tattributesFile = {tmp_path / 'attributes'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    _git(repo, "config", "core.attributesFile", str(tmp_path / "attributes"))

    assert tracked_files(repo) == (RepoPath("keep"), RepoPath("kept"), RepoPath("kept-too"))


def test_a_git_template_s_info_attributes_dont_decide_what_export_ignore_marks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo", {".gitattributes": "a export-ignore\n", "a": "", "b": ""})
    (tmp_path / "template" / "info").mkdir(parents=True)
    (tmp_path / "template" / "info" / "attributes").write_text("b export-ignore\n")
    (tmp_path / "gitconfig").write_text(f"[init]\n\ttemplateDir = {tmp_path / 'template'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))

    assert tracked_files(repo) == (RepoPath(".gitattributes"), RepoPath("b"))


def test_git_common_dir_neither_decides_what_export_ignore_marks_nor_gets_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo", {"a": "", "b": ""})
    other = _repo(tmp_path / "other", {})
    (other / ".git" / "info").mkdir(exist_ok=True)
    (other / ".git" / "info" / "attributes").write_text("b export-ignore\n")
    config = (other / ".git" / "config").read_text()
    monkeypatch.setenv("GIT_COMMON_DIR", str(other / ".git"))
    listed = tracked_files(repo)
    monkeypatch.delenv("GIT_COMMON_DIR")

    assert RepoPath("b") in listed
    assert (other / ".git" / "config").read_text() == config


@pytest.mark.parametrize(("name", "value"), [("GIT_WORK_TREE", "."), ("GIT_ATTR_SOURCE", "HEAD")])
def test_a_repo_variable_in_the_environment_doesnt_stop_the_attribute_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    repo = _repo(tmp_path / "repo", {".gitattributes": "a export-ignore\n", "a": "", "b": ""})

    assert export_subst(repo, (RepoPath("b"),)) == ()
    monkeypatch.setenv(name, str(repo) if value == "." else value)

    assert export_subst(repo, (RepoPath("a"), RepoPath("b"))) == ()


def test_export_ignore_patterns_match_case_sensitively_as_a_tarball_built_on_linux_does(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", {".gitattributes": "A.txt export-ignore\nDocs/ export-ignore\n", "a.txt": "", "docs/x": ""})

    assert tracked_files(repo) == (RepoPath(".gitattributes"), RepoPath("a.txt"), RepoPath("docs/x"))


def test_export_subst_names_each_file_it_s_set_on_itself_among_those_given(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", ATTRIBUTED)

    assert export_subst(repo, tracked_files(repo)) == (RepoPath("v.txt"),)


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


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize(("make", "reason"), [("directory", "is a directory"), ("fifo", "isn't a regular file")])
def test_a_source_that_isnt_a_regular_file_isnt_copied_and_nothing_is(tmp_path: Path, make: str, reason: str) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "first.txt").write_text("first\n")
    if make == "directory":
        (source / "odd").mkdir()
    else:
        os.mkfifo(source / "odd")

    with pytest.raises(ApplyError, match=f"'odd' {reason} in the source, so nothing was changed"):
        apply([Change("copy", "first.txt"), Change("copy", "odd")], source, target)

    assert not target.exists()


def test_a_copy_from_another_path_names_that_path_when_it_refuses(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    (source / "pins").mkdir(parents=True)

    with pytest.raises(ApplyError, match="'pins' is a directory in the source"):
        apply([Change("copy", "backend/uv.lock", source="pins")], source, target)


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
        f"apply([Change('write', 'menu.ts', {ascii(TEXT)})], Path(sys.argv[1]), Path(sys.argv[1]) / 'target')\n"
    )
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    run = subprocess.run([sys.executable, "-c", script, str(tmp_path)], env=env, capture_output=True, text=True)

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


@pytest.fixture
def locked(inside: Path) -> Iterator[Path]:
    """A directory in the repo, holding ``f.py``, that fastro isn't allowed to search."""
    directory = inside / "locked"
    directory.mkdir()
    (directory / "f.py").write_text("")
    directory.chmod(0)
    yield directory
    directory.chmod(0o755)


def test_a_config_file_fastro_isnt_allowed_to_resolve_raises_instead_of_being_refused(inside: Path, locked: Path) -> None:
    (inside / "blueprint.toml").symlink_to("locked/f.py")

    with pytest.raises(PermissionError):
        unreadable_config(inside, "blueprint.toml")


def test_a_config_file_fastro_cant_tell_is_there_raises(inside: Path, locked: Path) -> None:
    with pytest.raises(PermissionError):
        unreadable_config(locked, "blueprint.toml")


def test_a_config_file_in_a_symlink_loop_is_refused(inside: Path) -> None:
    (inside / "blueprint.toml").symlink_to("blueprint.toml")

    assert unreadable_config(inside, "blueprint.toml") == (
        f"{inside / 'blueprint.toml'} has symlinks that can't be followed. fastro reads blueprint.toml only as a "
        "regular file inside the repo."
    )


def test_nothing_at_a_config_path_is_no_refusal(inside: Path) -> None:
    assert unreadable_config(inside, "blueprint.toml") is None


def test_a_tracked_file_fastro_cant_check_is_kept(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", {"locked/f.py": "", "open.py": ""})
    (repo / "locked").chmod(0)
    try:
        listed = tracked_files(repo)
    finally:
        (repo / "locked").chmod(0o755)

    assert [path.text for path in listed] == ["locked/f.py", "open.py"]


def test_a_tracked_file_whose_directory_became_a_file_is_left_out(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", {"pkg/f.py": "", "open.py": ""})
    shutil.rmtree(repo / "pkg")
    (repo / "pkg").write_text("")

    assert [path.text for path in tracked_files(repo)] == ["open.py"]


def test_check_gives_each_change_where_it_lands_and_what_it_copies_from(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "a.py").write_text("a\n")
    target.mkdir()
    found = [Change("copy", "pkg/a.py"), Change("write", "pkg/b.py", "b\n"), Change("delete", "pkg/c.py")]

    checked = check(found, source, target)

    root, origin = target.resolve(), source.resolve()
    assert checked == [
        (found[0], root / "pkg/a.py", origin / "pkg/a.py"),
        (found[1], root / "pkg/b.py", None),
        (found[2], root / "pkg/c.py", None),
    ]


def test_check_changes_nothing_even_when_it_refuses(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / "taken").mkdir(parents=True)
    found = [Change("write", "new/b.py", "b\n"), Change("write", "taken", "x\n")]

    with pytest.raises(ApplyError, match="'taken' is a directory in the target"):
        check(found, tmp_path, target)

    assert sorted(path.name for path in target.iterdir()) == ["taken"]


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize(
    ("make", "followed", "unfollowed"),
    [
        ("file", "file", "file"),
        ("directory", "directory", "directory"),
        ("fifo", "other", "other"),
        ("symlink", "file", "symlink"),
        ("dangling", None, "symlink"),
        ("loop", None, "symlink"),
        ("missing", None, None),
        ("under-a-file", None, None),
    ],
)
def test_file_kind_says_what_is_at_a_path(tmp_path: Path, make: str, followed: str | None, unfollowed: str | None) -> None:
    (tmp_path / "real.txt").write_text("x\n")
    path = tmp_path / "thing"
    if make == "file":
        path.write_text("x\n")
    elif make == "directory":
        path.mkdir()
    elif make == "fifo":
        os.mkfifo(path)
    elif make == "symlink":
        path.symlink_to("real.txt")
    elif make == "dangling":
        path.symlink_to("nothing-here")
    elif make == "loop":
        path.symlink_to("thing")
    elif make == "under-a-file":
        path = tmp_path / "real.txt" / "thing"

    assert (file_kind(path), file_kind(path, follow=False)) == (followed, unfollowed)


@pytest.mark.parametrize("follow", [True, False])
def test_file_kind_raises_under_a_directory_fastro_cant_search_on_every_python(tmp_path: Path, follow: bool) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "file.txt").write_text("x\n")
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            file_kind(locked / "file.txt", follow=follow)
    finally:
        locked.chmod(0o755)


@pytest.mark.parametrize("side", ["target", "source"])
def test_check_raises_for_a_path_under_a_directory_fastro_cant_search_on_every_python(tmp_path: Path, side: str) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    (source / "locked").mkdir(parents=True)
    (target / "locked").mkdir(parents=True)
    (source / "locked" / "app.py").write_text("x = 1\n")
    found = [Change("copy", "locked/app.py")] if side == "source" else [Change("write", "locked/app.py", "x = 1\n")]
    locked = (source if side == "source" else target) / "locked"
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            check(found, source, target)
    finally:
        locked.chmod(0o755)
