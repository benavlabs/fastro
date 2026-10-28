"""``python -m fastro.engine.blueprint PATH``: track B's early check of its ``blueprint.toml``."""

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.problems import CatalogError

GOOD = """\
[blueprint]
name = "mini"
version = "0.1.0"
schema = 1
requires_fastro = ">=0.1.0.dev0"

[presets]
minimal = ["accounts"]

[points.routers]
cardinality = "many"

[features.accounts]
summary = "Users and login"
"""

MINIMAL = GOOD[: GOOD.index("[presets]")]

SHADOW = "whether a [commands] entry shadows a built-in fastro command"


def metadata_version() -> Version:
    return Version(metadata.version("fastro"))


def _repo(tmp_path: Path, text: str | bytes) -> Path:
    file = tmp_path / "blueprint.toml"
    if isinstance(text, bytes):
        file.write_bytes(text)
    else:
        file.write_text(text)
    return tmp_path


def test_a_good_catalog_says_ok_and_what_it_hasnt_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD)

    assert blueprint.main([str(repo)]) == 0
    assert capsys.readouterr() == (
        f"ok: {repo / 'blueprint.toml'} is mini 0.1.0, with 1 feature, 1 point and 1 preset.\n{blueprint.not_checked()}\n",
        "",
    )


@pytest.mark.parametrize("section", sorted(blueprint.TOP_LEVEL - {"blueprint"}))
def test_every_section_is_either_checked_or_listed_as_not_checked_yet(section: str) -> None:
    array = section == "wiring"
    text = MINIMAL + (f"[[{section}]]" if array else f"[{section}]") + "\nfastro_probe = 1\n"

    if section in blueprint.NOT_YET:
        assert blueprint.load("blueprint.toml", text, metadata_version()).info.name == "mini"
    else:
        probe = f"{section}[0].fastro_probe" if array else f"{section}.fastro_probe"
        with pytest.raises(CatalogError, match=re.escape(probe)):
            blueprint.load("blueprint.toml", text, metadata_version())


def test_the_blueprint_section_is_checked() -> None:
    with pytest.raises(CatalogError, match="blueprint.fastro_probe"):
        blueprint.load("blueprint.toml", MINIMAL + "fastro_probe = 1\n", metadata_version())


def test_every_section_is_checked_now_so_cross_references_and_shadowing_are_left() -> None:
    assert blueprint.NOT_YET == ()
    assert blueprint.not_checked() == (
        f"Not checked yet: names that refer to another section (features, points and options), and {SHADOW}."
    )


def test_a_command_that_shadows_a_built_in_loads_so_the_line_says_so() -> None:
    catalog = blueprint.load("blueprint.toml", MINIMAL + '[commands."make deploy"]\nmodule = "m"\n', metadata_version())

    assert ("make", "deploy") in catalog.commands
    assert "shadows a built-in fastro command" in blueprint.not_checked()


@pytest.mark.parametrize(
    ("sections", "cross", "line"),
    [
        (
            ("checks",),
            True,
            "Not checked yet: what's inside [checks], and names that refer to another section (features, points and "
            f"options), and {SHADOW}.",
        ),
        (
            ("wiring", "checks"),
            False,
            f"Not checked yet: what's inside [[wiring]] and [checks], and {SHADOW}.",
        ),
        (
            (),
            True,
            f"Not checked yet: names that refer to another section (features, points and options), and {SHADOW}.",
        ),
        ((), False, f"Not checked yet: {SHADOW}."),
    ],
)
def test_the_not_checked_line_shrinks_as_checks_arrive(
    sections: tuple[str, ...], cross: bool, line: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(blueprint, "NOT_YET", sections)
    monkeypatch.setattr(blueprint, "CROSS_REFERENCES_CHECKED", not cross)

    assert blueprint.not_checked() == line


def test_shadowing_is_still_named_once_everything_else_is_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(blueprint, "NOT_YET", ())
    monkeypatch.setattr(blueprint, "CROSS_REFERENCES_CHECKED", True)

    assert blueprint.main([str(_repo(tmp_path, GOOD))]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == [f"Not checked yet: {SHADOW}."]


def test_a_utf_8_byte_order_mark_is_read_past(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, b"\xef\xbb\xbf" + GOOD.encode())

    assert blueprint.main([str(repo)]) == 0
    assert capsys.readouterr().out.startswith("ok: ")


def test_a_bad_byte_after_a_byte_order_mark_is_reported_at_its_own_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, b"\xef\xbb\xbfa = 1\n\xff = 2\n")

    assert blueprint.main([str(repo)]) == 1
    assert capsys.readouterr().out.startswith(f"{repo / 'blueprint.toml'}:2: isn't UTF-8 text.\n")


def test_a_file_it_cant_read_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD)
    file = repo / "blueprint.toml"
    file.chmod(0)
    try:
        code = blueprint.main([str(repo)])
    finally:
        file.chmod(0o644)

    assert code == 3
    assert capsys.readouterr() == (
        "",
        f"error: can't read {file}: Permission denied. Check its permissions, then run "
        f"python -m fastro.engine.blueprint {repo} again.\n",
    )


def test_the_path_may_name_the_file_itself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    file = _repo(tmp_path, GOOD.replace('[presets]\nminimal = ["accounts"]\n', "")) / "blueprint.toml"

    assert blueprint.main([str(file)]) == 0
    assert capsys.readouterr().out.startswith(f"ok: {file} is mini 0.1.0, with 1 feature, 1 point and 0 presets.\n")


def test_problems_are_listed_with_a_count_and_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD + "require = []\ngroup = 1\n")
    file = repo / "blueprint.toml"

    assert blueprint.main([str(repo)]) == 1
    assert capsys.readouterr() == (
        f"{file}:15: features.accounts.require: isn't a key of [features.accounts].\n"
        "  Did you mean 'requires'?\n"
        f"{file}:16: features.accounts.group: should be a string, not an integer.\n"
        f"2 problems in {file}. Fix them, then run python -m fastro.engine.blueprint {repo} again.\n",
        "",
    )


def test_a_catalog_for_another_fastro_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD.replace(">=0.1.0.dev0", ">=99"))
    file = repo / "blueprint.toml"
    tool = metadata.version("fastro")

    assert blueprint.main([str(repo)]) == 3
    assert capsys.readouterr() == (
        f"{file}:5: blueprint.requires_fastro: is '>=99', which doesn't include this fastro, {tool}.\n"
        "  Upgrade fastro (uv tool upgrade fastro), then run this again.\n"
        f"1 problem in {file}. Fix it, then run python -m fastro.engine.blueprint {repo} again.\n",
        "",
    )


def test_a_file_that_isnt_utf_8_is_a_problem(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD.encode() + b'group = "\xff"\n')
    file = repo / "blueprint.toml"

    assert blueprint.main([str(repo)]) == 1
    assert capsys.readouterr().out.startswith(f"{file}:15: isn't UTF-8 text.\n  Save it as UTF-8, then run this again.\n")


@pytest.mark.parametrize(
    ("name", "why"),
    [("missing", "doesn't exist"), ("empty", "has no blueprint.toml")],
)
def test_a_path_without_a_catalog_is_a_usage_error(
    name: str, why: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "empty").mkdir()
    path = tmp_path / name

    assert blueprint.main([str(path)]) == 2
    assert capsys.readouterr() == (
        "",
        f"error: {path} {why}. Pass a blueprint repo or its blueprint.toml: python -m fastro.engine.blueprint PATH\n",
    )


@pytest.mark.parametrize("argv", [[], ["a", "b"]])
def test_anything_but_one_path_is_a_usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert blueprint.main(argv) == 2
    assert capsys.readouterr() == ("", "usage: python -m fastro.engine.blueprint PATH\n")


def test_without_fastros_metadata_it_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)

    assert blueprint.main([str(_repo(tmp_path, GOOD))]) == 3
    assert capsys.readouterr() == (
        "",
        "error: fastro isn't installed, so this can't tell which catalogs it reads. "
        "Install it (uv pip install -e PATH_TO_FASTRO), then run this again.\n",
    )


def test_it_runs_as_python_m_with_the_installed_fastros_version(tmp_path: Path) -> None:
    repo = _repo(tmp_path, GOOD)

    run = subprocess.run([sys.executable, "-m", "fastro.engine.blueprint", str(repo)], capture_output=True, text=True)

    assert (run.returncode, run.stderr) == (0, "")
    assert run.stdout.startswith("ok: ")


def test_it_exits_with_its_code_when_run_as_python_m(tmp_path: Path) -> None:
    run = subprocess.run([sys.executable, "-m", "fastro.engine.blueprint", str(tmp_path)], capture_output=True, text=True)

    assert run.returncode == 2
