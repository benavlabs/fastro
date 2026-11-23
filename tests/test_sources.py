"""Reading a source (spec 1.7): its kind, the default, named blueprints, the cache, and what a ``PATH`` contributes."""

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from fastro.engine.spec import RepoPath
from fastro.sources import NAMED, Source, SourceError, cache_dir, default_source, open_path, parse
from fastro.tools import MissingToolError

CATALOG = "blueprint.toml"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True).stdout


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def _committed(root: Path, files: dict[str, str]) -> Path:
    _tree(root, files)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x")
    return root


@pytest.mark.parametrize(
    ("environ", "blueprint", "given", "found"),
    [
        ({}, None, None, "fastapi-boilerplate"),
        ({"FASTRO_BLUEPRINT": ""}, None, None, "fastapi-boilerplate"),
        ({"FASTRO_BLUEPRINT": "../mine"}, None, None, "../mine"),
        ({"FASTRO_BLUEPRINT": "../mine"}, "fastroai", None, "fastroai"),
        ({"FASTRO_BLUEPRINT": "../mine"}, "fastroai", "gh:acme/bp@v1", "gh:acme/bp@v1"),
    ],
)
def test_the_default_source_is_fastro_blueprint_then_fastapi_boilerplate_and_the_flags_replace_it(
    environ: dict[str, str], blueprint: str | None, given: str | None, found: str
) -> None:
    assert default_source(environ, blueprint, given) == found


def test_the_named_blueprint_with_a_source_is_fastapi_boilerplate() -> None:
    assert NAMED == {"fastapi-boilerplate": "gh:benavlabs/FastAPI-boilerplate"}


@pytest.mark.parametrize("text", ["fastroai", "fastroai#sub"])
def test_fastroai_is_named_but_refused_until_its_repo_is_confirmed(text: str) -> None:
    with pytest.raises(SourceError, match=re.escape("'fastroai' is a named blueprint whose repo isn't confirmed yet")):
        parse(text)


@pytest.mark.parametrize(
    ("text", "source"),
    [
        ("fastapi-boilerplate", Source("gh", "benavlabs/FastAPI-boilerplate", None, None)),
        ("gh:acme/bp", Source("gh", "acme/bp", None, None)),
        ("gh:acme/bp@v1.2.0#templates/api", Source("gh", "acme/bp", "v1.2.0", "templates/api")),
        ("git+ssh://git@github.com/acme/bp", Source("git", "git+ssh://git@github.com/acme/bp", None, None)),
        ("git+ssh://git@github.com/acme/bp@main", Source("git", "git+ssh://git@github.com/acme/bp", "main", None)),
        ("git+https://example.com/a/b@v2#sub", Source("git", "git+https://example.com/a/b", "v2", "sub")),
        ("./here", Source("path", "./here", None, None)),
        ("../there#sub/dir", Source("path", "../there", None, "sub/dir")),
        ("/abs/path", Source("path", "/abs/path", None, None)),
        ("fastapi-boilerplate#sub", Source("gh", "benavlabs/FastAPI-boilerplate", None, "sub")),
        ("git+ssh://git@host:22/a@b/c@v1", Source("git", "git+ssh://git@host:22/a@b/c", "v1", None)),
    ],
)
def test_a_source_reads_as_its_kind_location_ref_and_subdirectory(text: str, source: Source) -> None:
    assert parse(text) == source


@pytest.mark.parametrize(
    "text", ["nope", "gh:acme", "./x#../up", "git+ssh://h/a/b@", "gh:acme/bp@", "fastapi-boilerplate#", "gh:../..", "gh:a/."]
)
def test_a_name_that_isnt_a_named_blueprint_or_a_source_that_doesnt_read_is_a_usage_error(text: str) -> None:
    with pytest.raises(SourceError, match=re.escape(repr(text))):
        parse(text)


@pytest.mark.parametrize(
    ("environ", "platform", "found"),
    [
        ({"FASTRO_CACHE_DIR": "/c"}, "darwin", "/c"),
        ({}, "darwin", "~/Library/Caches/fastro"),
        ({}, "linux", "~/.cache/fastro"),
        ({"XDG_CACHE_HOME": "/x"}, "linux", "/x/fastro"),
        ({"XDG_CACHE_HOME": "relative"}, "linux", "~/.cache/fastro"),
        ({"LOCALAPPDATA": "/l"}, "win32", "/l/fastro"),
        ({}, "win32", "~/AppData/Local/fastro"),
    ],
)
def test_the_cache_is_fastro_cache_dir_or_the_platform_s_cache_directory(
    environ: dict[str, str], platform: str, found: str
) -> None:
    assert cache_dir(environ, platform, Path("~")) == Path(found)


def test_a_path_that_isnt_a_git_work_tree_contributes_every_file_with_no_commit(tmp_path: Path) -> None:
    root = _tree(tmp_path / "bp", {CATALOG: "", "a.py": "", "pkg/b.py": "", ".hidden": ""})
    (root / "link").symlink_to(root / "a.py")
    (root / "linked").symlink_to(root / "pkg")

    tree = open_path(parse(str(root)))

    assert (tree.root, tree.commit) == (root.resolve(), None)
    assert tree.files == tuple(map(RepoPath, [".hidden", "a.py", CATALOG, "link", "linked", "pkg/b.py"]))


def test_a_path_in_a_git_work_tree_contributes_its_tracked_files_at_its_commit(tmp_path: Path) -> None:
    root = _committed(tmp_path / "bp", {CATALOG: "", "a.py": "", ".gitignore": "*.log\n"})
    (root / "notes.md").write_text("")
    (root / "run.log").write_text("")

    tree = open_path(parse(str(root)))
    with_untracked = open_path(parse(str(root)), include_untracked=True)

    assert tree.files == tuple(map(RepoPath, [".gitignore", "a.py", CATALOG]))
    assert with_untracked.files == tuple(map(RepoPath, [".gitignore", "a.py", CATALOG, "notes.md"]))
    assert tree.commit == _git(root, "rev-parse", "HEAD").strip()


def test_a_subdirectory_is_the_source_s_root_and_its_paths_are_relative_to_it(tmp_path: Path) -> None:
    repo = _committed(tmp_path / "mono", {".gitattributes": "bp/docs/ export-ignore\n", f"bp/{CATALOG}": "", "bp/a.py": ""})
    _tree(repo, {"bp/docs/d.md": "", "other.py": ""})
    _git(repo, "add", "-A")

    tree = open_path(parse(f"{repo}#bp"))

    assert (tree.root, tree.files) == ((repo / "bp").resolve(), (RepoPath("a.py"), RepoPath(CATALOG)))


def test_a_path_in_a_work_tree_with_no_commit_yet_is_a_usage_error(tmp_path: Path) -> None:
    root = _tree(tmp_path / "bp", {CATALOG: ""})
    _git(root, "init", "-q")

    with pytest.raises(SourceError, match="has no commit yet"):
        open_path(parse(str(root)))


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda root: None, "doesn't exist"),
        (lambda root: root.write_text(""), "bp isn't a directory. Pass a blueprint repo's directory."),
        (lambda root: root.mkdir(), "has no blueprint.toml"),
    ],
)
def test_a_path_that_isnt_a_blueprint_directory_is_a_usage_error(
    tmp_path: Path, make: Callable[[Path], None], message: str
) -> None:
    make(tmp_path / "bp")

    with pytest.raises(SourceError, match=message):
        open_path(parse(str(tmp_path / "bp")))


def test_a_subdirectory_that_leads_outside_the_path_is_a_usage_error(tmp_path: Path) -> None:
    _tree(tmp_path / "elsewhere", {CATALOG: ""})
    (tmp_path / "bp").mkdir()
    (tmp_path / "bp" / "sub").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(SourceError, match="leads outside"):
        open_path(parse(f"{tmp_path / 'bp'}#sub"))


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", ["outside", "fifo", "dangling", "directory"])
def test_a_catalog_1_10_rules_out_is_a_usage_error_saying_why(
    tmp_path: Path, replace: Callable[[Path, str, str], str], make: str
) -> None:
    root = _tree(tmp_path / "bp", {CATALOG: ""})
    reason = replace(root, CATALOG, make)

    with pytest.raises(SourceError, match=re.escape(f"{root / CATALOG} {reason}")):
        open_path(parse(str(root)))


def test_a_path_in_a_work_tree_needs_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _committed(tmp_path / "bp", {CATALOG: ""})
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(MissingToolError):
        open_path(parse(str(root)))


def test_a_path_inside_a_work_tree_s_subdirectory_counts_as_in_the_work_tree(tmp_path: Path) -> None:
    repo = _committed(tmp_path / "mono", {f"deep/bp/{CATALOG}": ""})
    (repo / "deep" / "bp" / "untracked.py").write_text("")

    assert open_path(parse(str(repo / "deep" / "bp"))).files == (RepoPath(CATALOG),)


@pytest.mark.parametrize(
    ("setup", "include_untracked"),
    [
        (lambda root: _committed(root, {".gitattributes": f"{CATALOG} export-ignore\n", CATALOG: ""}), False),
        (lambda root: (_committed(root, {"a": ""}), _tree(root, {CATALOG: ""})), False),
        (lambda root: (_committed(root, {"a": ""}), _tree(root, {f"sub/{CATALOG}": ""})), True),
    ],
    ids=["export-ignored", "untracked", "in-an-untracked-directory-even-with-untracked-files"],
)
def test_a_manifest_the_work_tree_doesn_t_contribute_is_a_usage_error(
    tmp_path: Path, setup: Callable[[Path], object], include_untracked: bool
) -> None:
    root = tmp_path / "bp"
    setup(root)
    _tree(root, {".gitignore": "sub/\n"})
    path = root / "sub" if (root / "sub").exists() else root

    with pytest.raises(
        SourceError, match=re.escape(f"{path.resolve() / CATALOG} isn't a file the source contributes")
    ) as caught:
        open_path(parse(str(path)), include_untracked=include_untracked)

    assert ("--include-untracked" in str(caught.value)) is not include_untracked


def test_an_untracked_manifest_is_contributed_with_untracked_files(tmp_path: Path) -> None:
    root = _committed(tmp_path / "bp", {"a": ""})
    _tree(root, {CATALOG: ""})

    assert open_path(parse(str(root)), include_untracked=True).files == (RepoPath("a"), RepoPath(CATALOG))


def test_a_path_git_says_isn_t_in_a_work_tree_is_a_usage_error_quoting_git(tmp_path: Path) -> None:
    repo = _committed(tmp_path / "repo", {"a": ""})
    _tree(repo / ".git" / "bp", {CATALOG: ""})
    broken = _tree(tmp_path / "broken", {CATALOG: "", ".git": "gitdir: /nowhere\n"})

    with pytest.raises(SourceError, match="isn't in a git work tree"):
        open_path(parse(str(repo / ".git" / "bp")))
    with pytest.raises(SourceError, match="isn't in a git work tree: fatal"):
        open_path(parse(str(broken)))


def test_a_directory_it_isn_t_allowed_to_read_isn_t_skipped(tmp_path: Path) -> None:
    root = _tree(tmp_path / "bp", {CATALOG: "", "locked/secret.py": ""})
    (root / "locked").chmod(0)
    try:
        with pytest.raises(PermissionError):
            open_path(parse(str(root)))
    finally:
        (root / "locked").chmod(0o755)


def test_a_relative_path_with_a_subdirectory_opens_at_its_resolved_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path / "bp", {CATALOG: "", f"sub/{CATALOG}": ""})
    (tmp_path / "link").symlink_to(tmp_path / "bp")
    monkeypatch.chdir(tmp_path)

    assert open_path(parse("./bp#sub")).root == (tmp_path / "bp" / "sub").resolve()
    assert open_path(parse("./link")).root == (tmp_path / "bp").resolve()


def test_a_subdirectory_that_isn_t_a_directory_is_a_usage_error(tmp_path: Path) -> None:
    root = _tree(tmp_path / "bp", {CATALOG: "", "sub": ""})

    with pytest.raises(SourceError, match=re.escape(f"{root}#sub isn't a directory. Name a directory inside {root}.")):
        open_path(parse(f"{root}#sub"))
