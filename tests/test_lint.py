"""``fastro blueprint lint``: a blueprint repo's load problems as findings, BP001 and BP003 (spec 2.13, 7, 8)."""

import json
import shutil
import subprocess
from importlib import metadata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fastro.cli import app

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
VERSION = metadata.version("fastro")
OWNED = 'paths = ["backend/src/modules/accounts/", "backend/tests/test_accounts.py"]'


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mini"
    shutil.copytree(MINI, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    monkeypatch.chdir(root)
    return root


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _lint(*arguments: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, ["blueprint", "lint", *arguments])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result.exit_code, result.stdout, result.stderr


def _findings(*arguments: str) -> list[dict[str, object]]:
    code, out, _ = _lint("--json", *arguments)
    assert code in (0, 1)
    return list(json.loads(out)["findings"])


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def test_a_clean_repo_has_no_findings(repo: Path) -> None:
    code, out, err = _lint()

    assert (code, err) == (0, "")
    assert out == f"ok: blueprint lint found nothing to fix in {repo}.\n"


def test_the_json_document_has_spec_7_s_shape(repo: Path) -> None:
    code, out, _ = _lint("--json")

    assert code == 0
    assert json.loads(out) == {
        "tool": "fastro",
        "version": VERSION,
        "schema": 1,
        "project": "mini",
        "blueprint": {"name": "mini", "version": "0.1.0"},
        "tiers": ["static"],
        "rules": ["BP001", "BP002", "BP003", "BP005"],
        "counts": {"error": 0, "warning": 0},
        "findings": [],
    }


def test_a_blueprint_toml_problem_is_a_bp001_finding_at_its_line(repo: Path) -> None:
    _edit(repo / "blueprint.toml", "schema = 1", 'schema = "one"')

    code, out, _ = _lint()

    assert code == 1
    assert out == (
        "blueprint.toml:4: BP001 blueprint.schema: should be an integer, not a string.\nblueprint lint found 1 problem.\n"
    )
    assert _findings() == [
        {
            "file": "blueprint.toml",
            "line": 4,
            "code": "BP001",
            "severity": "error",
            "message": "blueprint.schema: should be an integer, not a string.",
            "fix": "",
        }
    ]


def test_a_path_owned_twice_is_bp003_not_bp001(repo: Path) -> None:
    _edit(repo / "blueprint.toml", 'paths = ["backend/src/modules/cache/"', 'paths = ["backend/src/modules/accounts/"')

    (finding,) = _findings()

    assert (finding["code"], finding["file"]) == ("BP003", "blueprint.toml")
    assert "'backend/src/modules/accounts/' is declared by features.accounts.paths[0] already." in str(finding["message"])
    assert finding["fix"] == "A path belongs to one declaration: drop one of them."


def test_an_app_path_owned_twice_is_bp003_in_fastro_toml(repo: Path) -> None:
    (repo / "fastro.toml").write_text(
        (repo / "fastro.toml").read_text() + '\n[app]\npaths = ["backend/src/modules/accounts/"]\n'
    )

    (finding,) = _findings()

    assert (finding["code"], finding["file"]) == ("BP003", "fastro.toml")


def test_fastro_toml_is_checked_on_its_own_when_blueprint_toml_has_problems(repo: Path) -> None:
    _edit(repo / "blueprint.toml", "schema = 1", 'schema = "one"')
    _edit(repo / "fastro.toml", 'features = ["accounts"', 'features = 1, other = ["accounts"')

    found = _findings()

    assert [(finding["file"], finding["code"]) for finding in found][0] == ("blueprint.toml", "BP001")
    assert {finding["file"] for finding in found} == {"blueprint.toml", "fastro.toml"}


@pytest.mark.parametrize("pin", ['{ name = "mini", version = "0.1.0" }', '{ name = "other", version = "9.9.9" }'])
def test_a_blueprint_repo_s_fastro_toml_must_set_source_true(repo: Path, pin: str) -> None:
    _edit(repo / "fastro.toml", "source = true\n", "")
    _edit(repo / "fastro.toml", 'blueprint = { name = "mini", version = "0.1.0" }', f"blueprint = {pin}")

    (finding,) = _findings()

    assert (finding["file"], finding["code"], finding["line"]) == ("fastro.toml", "BP001", 1)
    assert finding["message"] == "source: isn't true, and a blueprint repo's fastro.toml sets source = true."
    assert finding["fix"] == "Add source = true to fastro.toml."


def test_a_repo_without_fastro_toml_is_linted_on_its_catalog_alone(repo: Path) -> None:
    (repo / "fastro.toml").unlink()

    code, out, _ = _lint("--json")

    assert code == 0
    assert json.loads(out)["project"] is None


def test_a_blueprint_toml_that_doesnt_load_leaves_the_blueprint_null(repo: Path) -> None:
    _edit(repo / "blueprint.toml", "schema = 1", 'schema = "one"')

    assert json.loads(_lint("--json")[1])["blueprint"] is None


def test_findings_are_sorted_by_file_line_and_code(repo: Path) -> None:
    _edit(repo / "blueprint.toml", "schema = 1", 'schema = "one"')
    _edit(repo / "blueprint.toml", 'version = "0.1.0"', "version = 1")
    _edit(repo / "fastro.toml", "source = true\n", "")

    found = [(finding["file"], finding["line"], finding["code"]) for finding in _findings()]

    assert found == sorted(found)
    assert len(found) == 3


def test_a_file_that_isnt_utf_8_is_a_bp001_finding(repo: Path) -> None:
    (repo / "blueprint.toml").write_bytes((repo / "blueprint.toml").read_bytes() + b"# \xff\n")

    (finding,) = _findings()

    assert (finding["file"], finding["code"], finding["message"]) == ("blueprint.toml", "BP001", "isn't UTF-8 text.")


def test_the_human_output_names_each_finding_and_its_fix(repo: Path) -> None:
    _edit(repo / "fastro.toml", "source = true\n", "")
    _edit(repo / "blueprint.toml", "schema = 1", 'schema = "one"')

    code, out, _ = _lint()

    assert code == 1
    assert out.splitlines() == [
        "blueprint.toml:4: BP001 blueprint.schema: should be an integer, not a string.",
        "fastro.toml:1: BP001 source: isn't true, and a blueprint repo's fastro.toml sets source = true.",
        "  Add source = true to fastro.toml.",
        "blueprint lint found 2 problems.",
    ]


def test_a_directory_without_blueprint_toml_is_a_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    code, _, err = _lint()

    assert code == 2
    assert f"{tmp_path} has no blueprint.toml" in err


def test_a_catalog_this_fastro_can_t_read_exits_3(repo: Path) -> None:
    _edit(repo / "blueprint.toml", 'requires_fastro = ">=0.1.0.dev0"', 'requires_fastro = ">=99"')

    code, out, err = _lint()

    assert (code, out) == (3, "")
    assert "requires_fastro" in err


@pytest.mark.parametrize("name", ["blueprint.toml", "fastro.toml"])
def test_a_file_that_cant_be_read_exits_3_naming_it(repo: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    read = Path.read_bytes

    def refuse(path: Path) -> bytes:
        if path.name == name:
            raise PermissionError(13, "Permission denied", str(path))
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", refuse)

    code, _, err = _lint()

    assert code == 3
    assert f"error: can't read {repo / name}: Permission denied." in err


@pytest.mark.parametrize("command", [["blueprint", "lint"], ["sync", "--check"]])
def test_without_fastro_installed_lint_and_sync_say_so_and_exit_3(
    repo: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)

    result = CliRunner().invoke(app, command)

    assert result.exit_code == 3
    assert "fastro isn't installed" in result.stderr


def test_findings_on_one_line_are_ordered_by_code_not_by_key(repo: Path) -> None:
    app = 'app = { requires = ["nope"], paths = ["backend/src/modules/accounts/"] }'
    _edit(repo / "fastro.toml", "\n[project]", f"\n{app}\n\n[project]")

    found = [(finding["line"], finding["code"], str(finding["message"]).split(":")[0]) for finding in _findings()]

    assert [code for _, code, _ in found] == ["BP001", "BP003"]
    assert found[0][0] == found[1][0]
    assert [key for _, _, key in found] == ["app.requires[0]", "app.paths[0]"]


def test_a_fastro_toml_that_reads_but_doesnt_fit_the_catalog_still_names_its_project(repo: Path) -> None:
    _edit(repo / "fastro.toml", 'features = ["accounts"', 'features = ["nope", "accounts"')

    code, out, _ = _lint("--json")
    document = json.loads(out)

    assert code == 1
    assert document["project"] == "mini"
    assert [finding["code"] for finding in document["findings"]] == ["BP001"]


def _append(repo: Path, text: str) -> None:
    (repo / "blueprint.toml").write_text((repo / "blueprint.toml").read_text() + "\n" + text)


def _track(repo: Path, *paths: str) -> None:
    for path in paths:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("")
    _git(repo, "add", *paths)


def test_a_declared_file_path_that_isnt_tracked_is_bp002_at_its_line(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", "backend/tests/test_accounts.py")

    (finding,) = _findings()

    assert (finding["file"], finding["code"]) == ("blueprint.toml", "BP002")
    assert finding["message"] == (
        "features.accounts.paths[1]: 'backend/tests/test_accounts.py' isn't a tracked file in the repo."
    )
    assert finding["fix"] == "Fix the path, or add the file to git."


def test_a_declared_file_path_deleted_from_the_working_tree_is_bp002(repo: Path) -> None:
    (repo / "backend/tests/test_accounts.py").unlink()

    assert [finding["code"] for finding in _findings()] == ["BP002"]


def test_a_declared_directory_that_holds_no_tracked_file_is_bp002(repo: Path) -> None:
    _git(repo, "rm", "-q", "-r", "--cached", "backend/src/modules/cache/")

    (finding,) = _findings()

    assert finding["code"] == "BP002"
    assert finding["message"] == "features.cache.paths[0]: 'backend/src/modules/cache/' holds no tracked file."


def test_a_when_s_paths_are_checked_too(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", "backend/src/modules/billing/paddle.py")

    (finding,) = _findings()

    assert finding["code"] == "BP002"
    assert str(finding["message"]).startswith("features.billing.when[0].paths[0]: ")


def test_a_generator_s_templates_and_the_overlay_are_directories_that_must_hold_a_tracked_file(repo: Path) -> None:
    _edit(
        repo / "blueprint.toml", 'requires_fastro = ">=0.1.0.dev0"\n', 'requires_fastro = ">=0.1.0.dev0"\noverlay = "overlay"\n'
    )
    _append(repo, '[generators.deploy]\ntemplates = "blueprint/deploy"\n')

    found = sorted(str(finding["message"]) for finding in _findings())

    assert found == [
        "blueprint.overlay: 'overlay/' holds no tracked file.",
        "generators.deploy.templates: 'blueprint/deploy/' holds no tracked file.",
    ]

    _track(repo, "overlay/README.md.j2", "blueprint/deploy/local/compose.yml")

    assert _findings() == []


def test_a_wiring_path_that_isnt_in_the_repo_isnt_bp002(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", "frontend/menu.ts")
    (repo / "frontend/menu.ts").unlink()

    assert _findings() == []


COMMAND = '[commands."db baseline"]\nmodule = "{module}"\n'


@pytest.mark.parametrize(
    ("module", "tracked"),
    [("scripts.baseline", "backend/scripts/baseline.py"), ("src.tools", "backend/src/tools/__init__.py")],
)
def test_a_command_whose_module_has_a_tracked_file_or_package_is_fine(repo: Path, module: str, tracked: str) -> None:
    _append(repo, COMMAND.format(module=module))
    _track(repo, tracked)

    assert _findings() == []


def test_a_command_whose_module_has_no_file_is_bp005_at_its_module(repo: Path) -> None:
    _append(repo, COMMAND.format(module="scripts.baseline"))

    (finding,) = _findings()

    assert (finding["code"], finding["line"]) == ("BP005", 192)
    assert finding["message"] == (
        "commands.\"db baseline\".module: 'scripts.baseline' has no file: neither backend/scripts/baseline.py nor "
        "backend/scripts/baseline/__init__.py is tracked."
    )
    assert finding["fix"] == "Point module at a module the repo has, or add its file to git."


def test_a_feature_command_s_module_is_checked_too(repo: Path) -> None:
    _append(repo, '[features.accounts.commands."users unlock"]\nmodule = "src.modules.accounts.unlock"\n')
    _track(repo, "backend/src/modules/accounts/__init__.py")

    (finding,) = _findings()

    assert finding["code"] == "BP005"
    assert "backend/src/modules/accounts/unlock.py" in str(finding["message"])


def test_a_command_s_module_is_resolved_from_its_cwd(repo: Path) -> None:
    _append(repo, '[commands."ui build"]\nmodule = "scripts.build"\ncwd = "frontend/"\n')
    _track(repo, "frontend/scripts/build.py")

    assert _findings() == []


def test_a_command_running_an_installed_package_isnt_checked(repo: Path) -> None:
    _append(repo, COMMAND.format(module="alembic.config"))

    assert _findings() == []


def test_a_catalog_that_doesnt_load_isnt_checked_against_the_files(repo: Path) -> None:
    _edit(repo / "blueprint.toml", "schema = 1", 'schema = "one"')
    _git(repo, "rm", "-q", "--cached", "backend/tests/test_accounts.py")

    assert [finding["code"] for finding in _findings()] == ["BP001"]


def test_without_git_lint_exits_3(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    code, out, err = _lint()

    assert (code, out) == (3, "")
    assert "git" in err


def test_a_directory_that_isnt_a_git_repo_is_a_usage_error(repo: Path) -> None:
    shutil.rmtree(repo / ".git")

    code, _, err = _lint()

    assert code == 2
    assert "isn't a git repository" in err
