"""A blueprint's overlay (spec 2.1, 6.2): its files reach a project at their path under the overlay, a ``*.j2`` file
rendered with the template context and its suffix dropped, and a plan that would give one path both an overlay
file and another source is refused (7). A ``blueprint verify`` build holds them too (2.13), named from the
blueprint's own ``pyproject.toml``."""

import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.apply import ApplyError
from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.verify import Unbuildable, build

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _repo(tmp_path: Path, files: Mapping[str, str | bytes], overlay: str | None = "overlay") -> tuple[Path, Blueprint]:
    """mini with ``files`` added and ``[blueprint].overlay`` set to ``overlay``, tracked by git, and its catalog."""
    root = tmp_path / "mini"
    shutil.copytree(MINI, root)
    text = (root / "blueprint.toml").read_text()
    if overlay is not None:
        text = text.replace(
            'requires_fastro = ">=0.1.0.dev0"\n', f'requires_fastro = ">=0.1.0.dev0"\noverlay = "{overlay}"\n', 1
        )
        (root / "blueprint.toml").write_text(text)
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (root / name).write_bytes(content)
        else:
            (root / name).write_text(content)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root, blueprint.load("blueprint.toml", text, Version("0.1.0"))


TEMPLATE = (
    "{{ project.name }} {{ project.slug }} {{ blueprint.name }} {{ blueprint.version }} {{ fastro.version }}\n"
    "{{ features | join(',') }}\n"
    "{{ options['cache.backend'] }} {{ options['billing.provider'] }} {{ has('cache') }} {{ has('tasks') }}"
    " {{ has('cache.backend=redis') }} {{ services }}\n"
    "<b>{{ '&' }}</b>\n"
)


def test_a_build_holds_the_overlay_rendered_with_the_template_context_and_the_blueprint_s_own_name(
    tmp_path: Path,
) -> None:
    root, catalog = _repo(
        tmp_path,
        {
            "overlay/README.md.j2": TEMPLATE,
            "overlay/docs/guide.md": "{{ not rendered }}\n",
            "overlay/bin/run.sh": "#!/bin/sh\n",
        },
    )
    (root / "overlay" / "bin" / "run.sh").chmod(0o755)
    _git(root, "add", "-A")

    project = build(root, catalog, ("billing", "cache"), tmp_path / "build", options={("cache", "backend"): "redis"})

    version = metadata.version("fastro")
    assert (project / "README.md").read_text() == (
        f"mini-backend mini-backend mini 0.1.0 {version}\n"
        "accounts,billing,cache\n"
        "redis stripe True False True ['redis']\n"
        "<b>&</b>\n"
    )
    assert (project / "docs" / "guide.md").read_text() == "{{ not rendered }}\n"
    assert os.stat(project / "bin" / "run.sh").st_mode & stat.S_IXUSR
    assert not (project / "README.md.j2").exists()
    assert not (project / "overlay").exists()


def test_a_template_keeps_its_last_newline_and_a_template_without_one_gets_none(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/a.txt.j2": "a\n\n", "overlay/b.txt.j2": "b"})

    project = build(root, catalog, (), tmp_path / "build")

    assert ((project / "a.txt").read_bytes(), (project / "b.txt").read_bytes()) == (b"a\n\n", b"b")


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{{ nothing }}\n", r"^overlay/a\.txt\.j2 doesn't render: 'nothing' is undefined"),
        ("{{ project.missing }}\n", r"^overlay/a\.txt\.j2 doesn't render: .*'missing'"),
        ("{% if %}\n", r"^overlay/a\.txt\.j2 doesn't render"),
        ("{{ has('nope') }}\n", r"^overlay/a\.txt\.j2 doesn't render: has\('nope'\): 'nope' isn't a feature"),
        (
            "{{ ''.__class__.__mro__[1].__subclasses__() }}\n",
            r"^overlay/a\.txt\.j2 doesn't render: access to attribute '__class__' of 'str' object is unsafe\.",
        ),
        ("{{ cycler.__init__.__globals__ }}\n", r"^overlay/a\.txt\.j2 doesn't render: .*unsafe"),
        ("{% set _ = project.update({'name': 'x'}) %}\n", r"^overlay/a\.txt\.j2 doesn't render: .*unsafe"),
        ("{% set _ = features.append('x') %}\n", r"^overlay/a\.txt\.j2 doesn't render: .*unsafe"),
        ('{% include "a.txt.j2" %}\n', r"^overlay/a\.txt\.j2 doesn't render: no loader for this environment specified$"),
        (
            '{% import "a.txt.j2" as other %}\n',
            r"^overlay/a\.txt\.j2 doesn't render: no loader for this environment specified$",
        ),
        ('{% extends "a.txt.j2" %}\n', r"^overlay/a\.txt\.j2 doesn't render: no loader for this environment specified$"),
    ],
)
def test_a_template_that_doesn_t_render_refuses_the_build_before_anything_is_written(
    tmp_path: Path, template: str, message: str
) -> None:
    root, catalog = _repo(tmp_path, {"overlay/a.txt.j2": template})

    with pytest.raises(Unbuildable, match=message):
        build(root, catalog, (), tmp_path / "build")

    assert not (tmp_path / "build").exists()


def test_a_template_that_isn_t_utf_8_refuses_the_build(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/a.txt.j2": b"\xff\n"})

    with pytest.raises(Unbuildable, match=r"^overlay/a\.txt\.j2 isn't UTF-8 text, so it can't be rendered\.$"):
        build(root, catalog, (), tmp_path / "build")


def test_a_template_read_past_a_byte_order_mark(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/a.txt.j2": b"\xef\xbb\xbf{{ blueprint.name }}\n"})

    assert (build(root, catalog, (), tmp_path / "build") / "a.txt").read_text() == "mini\n"


def test_a_template_that_s_a_symlink_refuses_the_build(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"real.j2": "x\n"})
    (root / "overlay").mkdir()
    (root / "overlay" / "a.txt.j2").symlink_to("../real.j2")
    _git(root, "add", "-A")

    with pytest.raises(Unbuildable, match=r"^overlay/a\.txt\.j2 is a symlink"):
        build(root, catalog, (), tmp_path / "build")


def test_two_overlay_files_at_one_project_path_refuse_the_build_naming_both(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/a.txt": "a\n", "overlay/a.txt.j2": "b\n"})

    with pytest.raises(Unbuildable, match=r"^overlay/a\.txt and overlay/a\.txt\.j2 would both give a project a\.txt"):
        build(root, catalog, (), tmp_path / "build")


@pytest.mark.parametrize(
    ("overlay_file", "other"),
    [
        ("overlay/backend/pyproject.toml", "backend/pyproject.toml"),
        ("overlay/backend/src/wiring/app.py.j2", "the wiring file backend/src/wiring/app.py"),
    ],
)
def test_an_overlay_file_at_a_path_the_plan_gives_another_source_refuses_the_build_naming_both(
    tmp_path: Path, overlay_file: str, other: str
) -> None:
    root, catalog = _repo(tmp_path, {overlay_file: "x\n"})
    path = overlay_file.removeprefix("overlay/").removesuffix(".j2")

    with pytest.raises(Unbuildable, match=f"^{overlay_file} and {other} would both give a project {path}"):
        build(root, catalog, (), tmp_path / "build")

    assert not (tmp_path / "build").exists()


def test_without_an_overlay_a_build_holds_none(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/a.txt": "a\n"}, overlay=None)

    project = build(root, catalog, (), tmp_path / "build")

    assert not (project / "a.txt").exists() and (project / "overlay" / "a.txt").exists()


@pytest.mark.parametrize("name", ["overlay/a.txt.j2", "overlay/b.txt"])
def test_an_overlay_file_export_subst_marks_refuses_the_build(tmp_path: Path, name: str) -> None:
    root, catalog = _repo(
        tmp_path, {"overlay/a.txt.j2": "a\n", "overlay/b.txt": "b\n", ".gitattributes": f"{name} export-subst\n"}
    )

    with pytest.raises(Unbuildable, match=f"^{name} has git's export-subst attribute set"):
        build(root, catalog, (), tmp_path / "build")


def test_an_overlay_env_example_is_processed_as_6_1_says(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/backend/.env.example.j2": "A={{ blueprint.name }}\n# @fastro nope\n"})

    with pytest.raises(Unbuildable, match=r"^overlay/backend/\.env\.example\.j2: line 2: .* isn't a marker fastro knows"):
        build(root, catalog, (), tmp_path / "build")


def test_an_overlay_env_example_is_what_the_build_holds_never_a_file_in_the_working_tree(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/backend/.env.example.j2": "FROM_OVERLAY={{ blueprint.name }}\n"})
    (root / "backend" / ".env.example").write_text("UNTRACKED=1\n")

    project = build(root, catalog, (), tmp_path / "build")

    assert (project / "backend" / ".env.example").read_text() == "FROM_OVERLAY=mini\n"


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {"overlay/backend": "x\n"},
            r"^overlay/backend and backend/\S+ would give a project both a file backend and files under it",
        ),
        (
            {"overlay/a.j2": "x\n", "overlay/a/b": "y\n"},
            r"^overlay/a\.j2 and overlay/a/b would give a project both a file a and files under it",
        ),
        (
            {"overlay/backend/pyproject.toml/x": "y\n"},
            r"^overlay/backend/pyproject\.toml/x and backend/pyproject\.toml would give a project both a file "
            r"backend/pyproject\.toml and files under it",
        ),
    ],
)
def test_one_path_a_file_from_one_source_and_a_directory_from_another_refuses_the_build_before_anything_is_written(
    tmp_path: Path, files: dict[str, str], message: str
) -> None:
    root, catalog = _repo(tmp_path, files)

    with pytest.raises(Unbuildable, match=message):
        build(root, catalog, (), tmp_path / "build")

    assert not (tmp_path / "build").exists()


@pytest.mark.parametrize("name", ["overlay/.j2", "overlay/d/.j2"])
def test_a_template_named_only_j2_gives_no_path_and_refuses_the_build(tmp_path: Path, name: str) -> None:
    root, catalog = _repo(tmp_path, {name: "x\n"})

    with pytest.raises(Unbuildable, match=f"^{name} is named only \\.j2, so it gives a project no path"):
        build(root, catalog, (), tmp_path / "build")


def test_a_template_that_isn_t_a_regular_file_in_the_working_tree_refuses_the_build(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/a.txt.j2": "a\n"})
    (root / "overlay" / "a.txt.j2").unlink()
    (root / "overlay" / "a.txt.j2").mkdir()

    with pytest.raises(Unbuildable, match=r"^overlay/a\.txt\.j2 isn't a regular file\. fastro reads an overlay template"):
        build(root, catalog, (), tmp_path / "build")


def test_an_overlay_file_that_s_a_symlink_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"real.txt": "x\n"})
    (root / "overlay").mkdir()
    (root / "overlay" / "s.txt").symlink_to("../real.txt")
    _git(root, "add", "-A")

    with pytest.raises(ApplyError, match=r"'overlay/s\.txt' is a symlink in the source"):
        build(root, catalog, (), tmp_path / "build")

    assert not (tmp_path / "build").exists()


def test_an_overlay_env_example_that_isn_t_utf_8_refuses_the_build(tmp_path: Path) -> None:
    root, catalog = _repo(tmp_path, {"overlay/backend/.env.example": b"\xff=1\n"})

    with pytest.raises(Unbuildable, match=r"^overlay/backend/\.env\.example isn't UTF-8 text\. Save it as UTF-8\.$"):
        build(root, catalog, (), tmp_path / "build")
