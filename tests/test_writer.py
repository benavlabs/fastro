"""``fastro new``'s project writer (spec 3, 4): the files a verify build would hold, plus ``fastro.toml``,
``.fastro/blueprint.toml``, the lock with each file's origin and sha256, and the base snapshot, every refusal made
before anything is written, and ``--force`` replacing only what it writes."""

import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.apply import gitlinks, tracked_files
from fastro.engine import blueprint, lock
from fastro.engine.blueprint import Blueprint
from fastro.engine.lock import Provenance
from fastro.engine.resolve import select
from fastro.engine.spec import RepoPath
from fastro.sources import Tree
from fastro.verify import build
from fastro.writer import Created, Refused, create

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
ORIGIN = Provenance("mini", Version("0.1.0"), "../mini", "a" * 40)
EXTRA = {"backend/.env.example": "A=1\n", "backend/uv.lock": "version = 1\n"}


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _source(tmp_path: Path, files: Mapping[str, str] = EXTRA, overlay: bool = False) -> tuple[Tree, Blueprint]:
    root = tmp_path / "mini"
    shutil.copytree(MINI, root)
    text = (root / "blueprint.toml").read_text()
    if overlay:
        text = text.replace('requires_fastro = ">=0.1.0.dev0"\n', 'requires_fastro = ">=0.1.0.dev0"\noverlay = "overlay"\n')
        (root / "blueprint.toml").write_text(text)
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    _git(root, "init", "-q")
    _git(root, "add", "-A", "-f")
    return Tree(root, tracked_files(root), None), blueprint.load("blueprint.toml", text, Version("0.1.0"))


def _create(
    tree: Tree,
    catalog: Blueprint,
    target: Path,
    asked: tuple[str, ...] = ("billing", "cache"),
    chosen: Mapping[tuple[str, str], str] | None = None,
    name: str = "acme",
    force: bool = False,
) -> Created:
    chosen = {} if chosen is None else chosen
    selection = select(catalog, asked, options=chosen)
    return create(tree, catalog, selection, asked, chosen, name, target, ORIGIN, force=force)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _locked(target: Path) -> lock.Lock:
    return lock.load(".fastro/lock.toml", (target / ".fastro" / "lock.toml").read_text())


def test_new_writes_what_a_verify_build_holds_and_fastro_s_own_files_beside_it(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    built = _files(build(tree.root, catalog, ("billing", "cache"), tmp_path / "build"))

    created = _create(tree, catalog, tmp_path / "acme", name="mini-backend")

    project = _files(tmp_path / "acme")
    assert {path: data for path, data in project.items() if path in built} == built
    assert sorted(set(project) - set(built)) == sorted(
        ["fastro.toml", ".fastro/blueprint.toml", ".fastro/lock.toml", ".fastro/.gitattributes"]
        + [path for path in project if path.startswith(".fastro/base/files/")]
    )
    assert created == Created(tuple(sorted(project)), ())


def test_fastro_toml_holds_what_the_user_asked_for_and_the_options_they_chose(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)

    _create(tree, catalog, tmp_path / "acme", asked=("cache", "billing"), chosen={("cache", "backend"): "redis"})

    assert (tmp_path / "acme" / "fastro.toml").read_text() == (
        'blueprint = { name = "mini", version = "0.1.0" }\n'
        'features = ["billing", "cache"]\n'
        "\n"
        "[project]\n"
        'name = "acme"\n'
        "\n"
        "[options]\n"
        '"cache.backend" = "redis"\n'
    )


def test_fastro_toml_has_no_options_table_when_the_user_chose_none(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)

    _create(tree, catalog, tmp_path / "acme", asked=("billing",))

    assert (tmp_path / "acme" / "fastro.toml").read_text() == (
        'blueprint = { name = "mini", version = "0.1.0" }\nfeatures = ["billing"]\n\n[project]\nname = "acme"\n'
    )


def test_the_lock_records_the_closed_selection_every_option_and_each_file_s_origin_and_sha256(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    target = tmp_path / "acme"

    _create(tree, catalog, target, chosen={("cache", "backend"): "redis"})

    locked = _locked(target)
    assert (locked.schema, locked.fastro, locked.blueprint) == (1, Version(metadata.version("fastro")), ORIGIN)
    assert locked.features == ("accounts", "billing", "cache")
    assert dict(locked.options) == {("billing", "provider"): "stripe", ("cache", "backend"): "redis"}
    origins = {path.text: entry.origin for path, entry in locked.files.items()}
    assert {path: origins[path] for path in ("backend/src/app.py", "backend/src/modules/billing/routes.py")} == {
        "backend/src/app.py": "core",
        "backend/src/modules/billing/routes.py": "feature:billing",
    }
    assert (origins["backend/src/wiring/app.py"], origins["backend/pyproject.toml"]) == ("wiring", "deps")
    assert origins["backend/.env.example"] == "env"
    assert {path.text: entry.sha256 for path, entry in locked.files.items()} == {
        path: hashlib.sha256((target / path).read_bytes()).hexdigest() for path in origins
    }
    written = set(_files(target))
    assert written - set(origins) == {
        "fastro.toml",
        "backend/uv.lock",
        ".fastro/blueprint.toml",
        ".fastro/lock.toml",
        ".fastro/.gitattributes",
        *(path for path in written if path.startswith(".fastro/base/files/")),
    }


def test_the_snapshot_holds_each_recorded_file_but_wiring_and_git_never_converts_it(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    target = tmp_path / "acme"

    _create(tree, catalog, target)

    locked = _locked(target)
    snapshot = {
        path.removeprefix(".fastro/base/files/").removesuffix(".base"): data
        for path, data in _files(target).items()
        if path.startswith(".fastro/base/files/")
    }
    assert snapshot == {
        path.text: (target / path.text).read_bytes() for path, entry in locked.files.items() if entry.origin != "wiring"
    }
    assert (target / ".fastro" / ".gitattributes").read_text() == "* -text\n"


def test_the_catalog_is_kept_as_the_source_holds_it_and_the_lock_seeds_the_project_s(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    target = tmp_path / "acme"

    _create(tree, catalog, target)

    assert (target / ".fastro" / "blueprint.toml").read_bytes() == (tree.root / "blueprint.toml").read_bytes()
    assert (target / "backend" / "uv.lock").read_text() == "version = 1\n"


def test_an_overlay_file_is_rendered_for_the_project_s_name_and_recorded_as_overlay(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path, {**EXTRA, "overlay/README.md.j2": "# {{ project.name }}\n"}, overlay=True)
    target = tmp_path / "acme"

    _create(tree, catalog, target)

    assert (target / "README.md").read_text() == "# acme\n"
    assert _locked(target).files[RepoPath("README.md")].origin == "overlay"


def _refused(tree: Tree, catalog: Blueprint, target: Path, message: str, force: bool = False) -> None:
    before = _files(target) if target.is_dir() else None
    with pytest.raises(Refused) as caught:
        _create(tree, catalog, target, force=force)
    assert str(caught.value) == message
    assert (_files(target) if target.is_dir() else None) == before


def test_a_target_that_s_a_file_is_refused_with_or_without_force(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    (tmp_path / "acme").write_text("x\n")
    message = (
        f"{tmp_path / 'acme'} is a file, and fastro new writes a project into a directory. Name one that's empty or "
        "doesn't exist yet."
    )

    _refused(tree, catalog, tmp_path / "acme", message)
    _refused(tree, catalog, tmp_path / "acme", message, force=True)
    assert (tmp_path / "acme").read_text() == "x\n"


def test_a_target_that_s_a_fifo_is_refused(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    os.mkfifo(tmp_path / "acme")

    with pytest.raises(Refused, match="is a file, and fastro new writes a project into a directory"):
        _create(tree, catalog, tmp_path / "acme", force=True)


def test_a_target_that_isn_t_empty_is_refused_without_force(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "notes.txt").write_text("mine\n")

    _refused(
        tree,
        catalog,
        tmp_path / "acme",
        f"{tmp_path / 'acme'} isn't empty. Name an empty directory, or pass --force to write into this one.",
    )


def test_a_env_a_project_would_receive_is_refused_naming_each(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path, {**EXTRA, "notes/.env": "SECRET=1\n", "d/.env": "SECRET=2\n"})

    _refused(
        tree,
        catalog,
        tmp_path / "acme",
        "d/.env and notes/.env would reach the project as a .env, which is almost always a leaked secret, so nothing "
        "was written. Remove them from the blueprint: fastro new writes a project's .env itself.",
    )
    assert not (tmp_path / "acme").exists()


def test_a_file_the_source_contributes_at_a_path_that_isn_t_utf_8_is_refused_whatever_the_selection(
    tmp_path: Path,
) -> None:
    tree, catalog = _source(tmp_path)
    odd = Tree(tree.root, (*tree.files, RepoPath("backend/src/modules/tasks/caf\udce9.py")), None)

    _refused(
        odd,
        catalog,
        tmp_path / "acme",
        "backend/src/modules/tasks/caf\\xe9.py is at a path that isn't UTF-8, which no source can carry, so nothing "
        "was written. Rename it to a path that's UTF-8.",
    )


def test_a_file_export_subst_marks_is_refused(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    selection = select(catalog, ("billing",))

    with pytest.raises(Refused, match=r"^backend/src/app\.py has git's export-subst attribute set"):
        create(
            tree,
            catalog,
            selection,
            ("billing",),
            {},
            "acme",
            tmp_path / "acme",
            ORIGIN,
            marked=lambda files: [path for path in files if path.text == "backend/src/app.py"],
        )

    assert not (tmp_path / "acme").exists()


def test_two_snapshots_where_one_would_be_a_directory_the_other_sits_under_are_refused(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path, {**EXTRA, "backend/src/x.py": "", "backend/src/x.py.base/y.py": ""})

    _refused(
        tree,
        catalog,
        tmp_path / "acme",
        "backend/src/x.py and backend/src/x.py.base/y.py would both have a snapshot, and backend/src/x.py's, "
        ".fastro/base/files/backend/src/x.py.base, would be a directory the other's sits under, so nothing was "
        "written.",
    )


def test_force_replaces_each_file_it_writes_as_a_new_file_and_keeps_the_rest(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    target = tmp_path / "acme"
    (target / "backend").mkdir(parents=True)
    outside = tmp_path / "outside.toml"
    outside.write_text("mine\n")
    os.link(outside, target / "backend" / "pyproject.toml")
    (target / "notes.txt").write_text("mine\n")
    (target / ".env").write_text("SECRET=kept\n")
    (tmp_path / "fresh").write_text("")
    outside.chmod(0o755)

    created = _create(tree, catalog, target, force=True)

    assert created.replaced == ("backend/pyproject.toml",)
    assert (target / "backend" / "pyproject.toml").stat().st_mode & 0o777 == (tmp_path / "fresh").stat().st_mode & 0o777
    assert outside.read_text() == "mine\n"
    assert "[project]" in (target / "backend" / "pyproject.toml").read_text()
    assert (target / "notes.txt").read_text() == "mine\n" and (target / ".env").read_text() == "SECRET=kept\n"
    assert "notes.txt" not in created.written and ".env" not in created.written


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (
            "symlink",
            "'backend/pyproject.toml' is a symlink in the target, and apply doesn't follow one, so nothing was changed.",
        ),
        ("directory", "'backend/pyproject.toml' is a directory in the target, so nothing was changed."),
    ],
)
def test_force_refuses_a_symlink_or_a_directory_where_it_would_write(tmp_path: Path, make: str, message: str) -> None:
    tree, catalog = _source(tmp_path)
    target = tmp_path / "acme"
    (target / "backend").mkdir(parents=True)
    if make == "symlink":
        (tmp_path / "elsewhere.toml").write_text("x\n")
        (target / "backend" / "pyproject.toml").symlink_to(tmp_path / "elsewhere.toml")
    else:
        (target / "backend" / "pyproject.toml").mkdir()

    _refused(tree, catalog, target, message, force=True)
    if make == "symlink":
        assert (tmp_path / "elsewhere.toml").read_text() == "x\n"


def test_a_path_that_isn_t_utf_8_under_blueprint_only_isn_t_contributed_so_isn_t_refused(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    kept = Tree(tree.root, (*tree.files, RepoPath(".fastro/caf\udce9.md")), None)

    _create(kept, catalog, tmp_path / "acme")

    assert (tmp_path / "acme" / "fastro.toml").exists()


def test_new_into_its_own_source_is_refused_and_deletes_nothing(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    before = _files(tree.root)

    with pytest.raises(Refused) as caught:
        _create(tree, catalog, tree.root, force=True)

    assert str(caught.value) == (
        f"{tree.root} is the source's own directory, and fastro new won't write over the blueprint it reads, so "
        "nothing was written. Name another directory."
    )

    assert _files(tree.root) == before


def test_a_env_no_project_receives_isn_t_refused(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path, {**EXTRA, ".fastro/.env": "SECRET=1\n"})

    _create(tree, catalog, tmp_path / "acme")

    assert not (tmp_path / "acme" / ".fastro" / ".env").exists()


def test_a_submodule_named_env_isn_t_taken_for_a_env(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    (tree.root / "vendor" / ".env").mkdir(parents=True)
    _git(tree.root, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/.env")
    files = tracked_files(tree.root)
    selection = select(catalog, ("billing",))

    with pytest.raises(Refused) as caught:
        create(
            Tree(tree.root, files, None),
            catalog,
            selection,
            ("billing",),
            {},
            "acme",
            tmp_path / "acme",
            ORIGIN,
            submodules=gitlinks(tree.root),
        )

    assert str(caught.value) == "'vendor/.env' is a directory in the source, so nothing was changed."


def test_a_wiring_file_has_no_snapshot_so_a_file_under_its_base_path_doesn_t_clash(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path, {**EXTRA, "backend/src/wiring/app.py.base/notes.md": "x\n"})

    _create(tree, catalog, tmp_path / "acme")

    assert (tmp_path / "acme" / ".fastro" / "base" / "files" / "backend/src/wiring/app.py.base/notes.md.base").exists()


def test_new_into_an_existing_empty_directory_needs_no_force(tmp_path: Path) -> None:
    tree, catalog = _source(tmp_path)
    (tmp_path / "acme").mkdir()

    _create(tree, catalog, tmp_path / "acme")

    assert (tmp_path / "acme" / "fastro.toml").exists()


@pytest.mark.parametrize(
    ("extra", "where"),
    [
        ({"frontend/backend/pyproject.toml": "mine = 1\n"}, "frontend"),
        ({"mini/blueprint.toml": "mine = 1\n"}, ".."),
        ({"frontend/fastro.toml": "mine = 1\n"}, "frontend"),
        ({"frontend/.fastro/lock.toml": "mine = 1\n"}, "frontend"),
        ({"frontend/.fastro/blueprint.toml": "mine = 1\n"}, "frontend"),
    ],
    ids=["inside", "above", "fastro-toml", "lock", "kept-catalog"],
)
def test_new_never_writes_over_a_file_of_its_own_source(tmp_path: Path, extra: dict[str, str], where: str) -> None:
    tree, catalog = _source(tmp_path, {**EXTRA, **extra})
    target = (tree.root / where).resolve()
    before = _files(tree.root)

    with pytest.raises(Refused, match="is one of the source's own files, and fastro new won't write over the blueprint"):
        _create(tree, catalog, target, force=True)

    assert _files(tree.root) == before


def test_new_into_the_source_spelled_another_way_is_refused_where_the_file_system_takes_it_for_the_same(
    tmp_path: Path,
) -> None:
    tree, catalog = _source(tmp_path)
    alias = tree.root.parent / tree.root.name.upper()
    before = _files(tree.root)
    same = alias.exists()

    if same:
        with pytest.raises(Refused, match="is the source's own directory"):
            _create(tree, catalog, alias, force=True)
    else:
        _create(tree, catalog, alias, force=True)

    assert _files(tree.root) == before
