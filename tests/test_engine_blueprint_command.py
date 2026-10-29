"""``python -m fastro.engine.blueprint PATH``: track B's early check of its ``blueprint.toml``."""

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint, catalog_check
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

[[wiring]]
path = "backend/src/routers.py"
doc = "Routers."

[[wiring.slots]]
kind = "tuple"
name = "ROUTERS"
point = "routers"
annotation = "tuple[object, ...]"

[features.accounts]
summary = "Users and login"
"""

MINIMAL = GOOD[: GOOD.index("[presets]")]

SHADOW = "whether a declared command shadows a built-in fastro command"
MODULES = "where a feature command's module sits"
POINTS = "which points contributions, slots and [plugin_api] name"


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

    assert catalog_check.main([str(repo)]) == 0
    assert capsys.readouterr() == (
        f"ok: {repo / 'blueprint.toml'} is mini 0.1.0, with 1 feature, 1 point and 1 preset.\n{catalog_check.not_checked()}\n",
        "",
    )


@pytest.mark.parametrize("section", sorted(blueprint.TOP_LEVEL - {"blueprint"}))
def test_every_section_is_either_checked_or_listed_as_not_checked_yet(section: str) -> None:
    array = section == "wiring"
    text = MINIMAL + (f"[[{section}]]" if array else f"[{section}]") + "\nfastro_probe = 1\n"

    if section in catalog_check.NOT_YET:
        assert blueprint.load("blueprint.toml", text, metadata_version()).info.name == "mini"
    else:
        probe = f"{section}[0].fastro_probe" if array else f"{section}.fastro_probe"
        with pytest.raises(CatalogError, match=re.escape(probe)):
            blueprint.load("blueprint.toml", text, metadata_version())


def test_the_blueprint_section_is_checked() -> None:
    with pytest.raises(CatalogError, match="blueprint.fastro_probe"):
        blueprint.load("blueprint.toml", MINIMAL + "fastro_probe = 1\n", metadata_version())


def test_every_section_and_cross_reference_is_checked_now_so_only_shadowing_is_left() -> None:
    assert catalog_check.NOT_YET == ()
    assert catalog_check.CROSS_REFERENCES_LEFT == ()
    assert catalog_check.not_checked() == f"Not checked yet: {SHADOW}."


def test_a_command_that_shadows_a_built_in_loads_so_the_line_says_so() -> None:
    catalog = blueprint.load("blueprint.toml", MINIMAL + '[commands."make deploy"]\nmodule = "m"\n', metadata_version())

    assert ("make", "deploy") in catalog.commands
    assert "shadows a built-in fastro command" in catalog_check.not_checked()


@pytest.mark.parametrize(
    ("sections", "left", "line"),
    [
        (
            ("checks",),
            (POINTS, MODULES),
            f"Not checked yet: what's inside [checks], {POINTS}, {MODULES} and {SHADOW}.",
        ),
        (
            ("wiring", "checks"),
            (),
            f"Not checked yet: what's inside [[wiring]] and [checks] and {SHADOW}.",
        ),
        ((), (MODULES,), f"Not checked yet: {MODULES} and {SHADOW}."),
        ((), (), f"Not checked yet: {SHADOW}."),
    ],
)
def test_the_not_checked_line_shrinks_as_checks_arrive(
    sections: tuple[str, ...], left: tuple[str, ...], line: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_check, "NOT_YET", sections)
    monkeypatch.setattr(catalog_check, "CROSS_REFERENCES_LEFT", left)

    assert catalog_check.not_checked() == line


def test_shadowing_is_still_named_once_everything_else_is_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_check, "NOT_YET", ())
    monkeypatch.setattr(catalog_check, "CROSS_REFERENCES_LEFT", ())

    assert catalog_check.main([str(_repo(tmp_path, GOOD))]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == [f"Not checked yet: {SHADOW}."]


def test_an_extending_catalog_with_a_base_name_says_ok_and_that_names_wait_for_the_merge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = GOOD.replace("[presets]", 'extends = { name = "fastro", version = "0.21.0" }\n\n[presets]').replace(
        'summary = "Users and login"', 'summary = "Users and login"\nrequires = ["jwt"]'
    )

    assert catalog_check.main([str(_repo(tmp_path, text))]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == [
        "Not checked yet: names that refer to another section, since this catalog extends fastro, and those are "
        f"checked on the merged catalog (extends, not built yet) and {SHADOW}."
    ]


def test_a_utf_8_byte_order_mark_is_read_past(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, b"\xef\xbb\xbf" + GOOD.encode())

    assert catalog_check.main([str(repo)]) == 0
    assert capsys.readouterr().out.startswith("ok: ")


def test_a_bad_byte_after_a_byte_order_mark_is_reported_at_its_own_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, b"\xef\xbb\xbfa = 1\n\xff = 2\n")

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr().out.startswith(f"{repo / 'blueprint.toml'}:2: isn't UTF-8 text.\n")


def _unreadable(file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    read_bytes = Path.read_bytes

    def refused(self: Path) -> bytes:
        if self == file:
            raise PermissionError(13, "Permission denied", str(self))
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", refused)


def test_a_file_it_cant_read_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, GOOD)
    file = repo / "blueprint.toml"
    _unreadable(file, monkeypatch)

    code = catalog_check.main([str(repo)])

    assert code == 3
    assert capsys.readouterr() == (
        "",
        f"error: can't read {file}: Permission denied. Check its permissions, then run "
        f"python -m fastro.engine.blueprint {repo} again.\n",
    )


def test_the_path_may_name_the_file_itself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    file = _repo(tmp_path, GOOD.replace('[presets]\nminimal = ["accounts"]\n', "")) / "blueprint.toml"

    assert catalog_check.main([str(file)]) == 0
    assert capsys.readouterr().out.startswith(f"ok: {file} is mini 0.1.0, with 1 feature, 1 point and 0 presets.\n")


def test_problems_are_listed_with_a_count_and_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD + "require = []\ngroup = 1\n")
    file = repo / "blueprint.toml"

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr() == (
        f"{file}:25: features.accounts.require: isn't a key of [features.accounts].\n"
        "  Did you mean 'requires'?\n"
        f"{file}:26: features.accounts.group: should be a string, not an integer.\n"
        "Names that refer to another section aren't checked until these are fixed.\n"
        f"2 problems in {file}. Fix them, then run python -m fastro.engine.blueprint {repo} again.\n",
        "",
    )


@pytest.mark.parametrize(
    ("version", "extra", "problem"),
    [
        ("0.21.0", "group = 1\n", "27: features.accounts.group: should be a string, not an integer."),
        (">=0.21", "", "7: blueprint.extends.version: '>=0.21' isn't a PEP 440 version, such as '0.21.0'."),
    ],
)
def test_key_problems_in_an_extending_catalog_come_without_the_note_about_names(
    version: str, extra: str, problem: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extends = f'extends = {{ name = "fastro", version = "{version}" }}\n\n[presets]'
    repo = _repo(tmp_path, GOOD.replace("[presets]", extends) + extra)
    file = repo / "blueprint.toml"

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr() == (
        f"{file}:{problem}\n1 problem in {file}. Fix it, then run python -m fastro.engine.blueprint {repo} again.\n",
        "",
    )


def test_problems_with_names_come_without_the_note_about_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD + 'requires = ["acounts"]\n')
    file = repo / "blueprint.toml"

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr() == (
        f"{file}:25: features.accounts.requires[0]: 'acounts' isn't a feature of this catalog.\n"
        "  Did you mean 'accounts'?\n"
        f"1 problem in {file}. Fix it, then run python -m fastro.engine.blueprint {repo} again.\n",
        "",
    )


def test_a_catalog_for_another_fastro_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD.replace(">=0.1.0.dev0", ">=99"))
    file = repo / "blueprint.toml"
    tool = metadata.version("fastro")

    assert catalog_check.main([str(repo)]) == 3
    assert capsys.readouterr() == (
        f"{file}:5: blueprint.requires_fastro: is '>=99', which doesn't include this fastro, {tool}.\n"
        "  Upgrade fastro (uv tool upgrade fastro), then run this again.\n"
        f"1 problem in {file}. Fix it, then run python -m fastro.engine.blueprint {repo} again.\n",
        "",
    )


def test_a_file_that_isnt_utf_8_is_a_problem(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path, GOOD.encode() + b'group = "\xff"\n')
    file = repo / "blueprint.toml"

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr().out.startswith(f"{file}:25: isn't UTF-8 text.\n  Save it as UTF-8, then run this again.\n")


@pytest.mark.parametrize(
    ("name", "why"),
    [("missing", "doesn't exist"), ("empty", "has no blueprint.toml")],
)
def test_a_path_without_a_catalog_is_a_usage_error(
    name: str, why: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "empty").mkdir()
    path = tmp_path / name

    assert catalog_check.main([str(path)]) == 2
    assert capsys.readouterr() == (
        "",
        f"error: {path} {why}. Pass a blueprint repo or its blueprint.toml: python -m fastro.engine.blueprint PATH\n",
    )


@pytest.mark.parametrize("argv", [[], ["a", "b"]])
def test_anything_but_one_path_is_a_usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert catalog_check.main(argv) == 2
    assert capsys.readouterr() == ("", "usage: python -m fastro.engine.blueprint PATH\n")


def test_without_fastros_metadata_it_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)

    assert catalog_check.main([str(_repo(tmp_path, GOOD))]) == 3
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


PROJECT = """\
blueprint = { name = "mini", version = "0.1.0" }
source = true
features = ["accounts"]

[project]
name = "mini-app"
"""


def _with_project(tmp_path: Path, project: str | bytes, catalog: str = GOOD) -> Path:
    repo = _repo(tmp_path, catalog)
    file = repo / "fastro.toml"
    if isinstance(project, bytes):
        file.write_bytes(project)
    else:
        file.write_text(project)
    return repo


def test_a_fastro_toml_next_to_the_catalog_is_checked_against_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _with_project(tmp_path, PROJECT)

    assert catalog_check.main([str(repo)]) == 0
    assert capsys.readouterr() == (
        f"ok: {repo / 'blueprint.toml'} is mini 0.1.0, with 1 feature, 1 point and 1 preset.\n"
        f"ok: {repo / 'fastro.toml'} is mini-app, with 1 feature, for mini 0.1.0.\n"
        f"Not checked yet: {SHADOW}.\n",
        "",
    )


def test_the_fastro_toml_next_to_a_catalog_file_given_by_name_is_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _with_project(tmp_path, PROJECT.replace('["accounts"]', "[]"))

    assert catalog_check.main([str(repo / "blueprint.toml")]) == 0
    assert (
        capsys.readouterr().out.splitlines()[1] == f"ok: {repo / 'fastro.toml'} is mini-app, with 0 features, for mini 0.1.0."
    )


def test_key_problems_in_fastro_toml_are_listed_with_their_own_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _with_project(tmp_path, PROJECT.replace('"mini-app"', '"-app"'))
    file = repo / "fastro.toml"

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr() == (
        f"ok: {repo / 'blueprint.toml'} is mini 0.1.0, with 1 feature, 1 point and 1 preset.\n"
        f"{file}:6: project.name: '-app' isn't a project name: use letters, digits, '.', '_' and '-', "
        "starting and ending with a letter or digit, as PEP 508 says.\n"
        "Names that refer to the catalog aren't checked until these are fixed.\n"
        f"1 problem in {file}. Fix it, then run python -m fastro.engine.blueprint {repo} again.\n",
        "",
    )


def test_names_in_fastro_toml_that_the_catalog_doesnt_declare_come_without_the_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _with_project(tmp_path, PROJECT.replace('"0.1.0"', '"0.2.0"'))
    file = repo / "fastro.toml"

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        f"{file}:1: blueprint.version: is 0.2.0, and the catalog is 0.1.0.",
        "  Set blueprint.version in fastro.toml to 0.1.0, the catalog's version.",
        f"1 problem in {file}. Fix it, then run python -m fastro.engine.blueprint {repo} again.",
    ]


def test_with_catalog_problems_the_fastro_toml_waits_for_them(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _with_project(tmp_path, PROJECT, GOOD + "group = 1\n")
    file = repo / "blueprint.toml"

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        "Names that refer to another section aren't checked until these are fixed.",
        f"{repo / 'fastro.toml'} is checked once {file} has no problems.",
        f"1 problem in {file}. Fix it, then run python -m fastro.engine.blueprint {repo} again.",
    ]


def test_a_fastro_toml_that_isnt_utf_8_is_a_problem(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _with_project(tmp_path, PROJECT.encode() + b"# \xff\n")

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr().out.splitlines()[1] == f"{repo / 'fastro.toml'}:7: isn't UTF-8 text."


def test_a_fastro_toml_it_cant_read_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _with_project(tmp_path, PROJECT)
    file = repo / "fastro.toml"
    _unreadable(file, monkeypatch)

    code = catalog_check.main([str(repo)])

    assert code == 3
    assert capsys.readouterr().err == (
        f"error: can't read {file}: Permission denied. Check its permissions, then run "
        f"python -m fastro.engine.blueprint {repo} again.\n"
    )


def test_against_an_extending_catalog_key_problems_in_fastro_toml_come_without_the_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = GOOD.replace("[presets]", 'extends = { name = "fastro", version = "0.21.0" }\n\n[presets]')
    repo = _with_project(tmp_path, PROJECT.replace('"mini-app"', '"-app"'), catalog)
    file = repo / "fastro.toml"

    assert catalog_check.main([str(repo)]) == 1
    assert capsys.readouterr().out.splitlines()[2:] == [
        f"1 problem in {file}. Fix it, then run python -m fastro.engine.blueprint {repo} again.",
    ]


@pytest.mark.parametrize("source", ["source = false\n", ""])
def test_a_fastro_toml_without_source_true_isnt_checked_against_this_catalog(
    source: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _with_project(tmp_path, PROJECT.replace("source = true\n", source).replace('"0.1.0"', '"9.9.9"'))
    file, catalog = repo / "fastro.toml", repo / "blueprint.toml"

    assert catalog_check.main([str(repo)]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == [
        f"{file} doesn't set source = true, so it pins .fastro/blueprint.toml, not {catalog}, "
        "and this command doesn't check it there. A blueprint repo's fastro.toml sets source = true.",
        f"Not checked yet: {SHADOW}.",
    ]
