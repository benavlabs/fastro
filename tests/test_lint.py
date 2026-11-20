"""``fastro blueprint lint``: a repo's problems as findings, BP001 to BP006, FR002, FR014 and FR016 (spec 2.13, 7, 8)."""

import json
import shutil
import subprocess
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fastro import lint as linting
from fastro.cli import app

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
VERSION = metadata.version("fastro")
EXTENDS: dict[str, tuple[str, str]] = {
    "named": ('extends = { name = "base", version = "1.0.0" }', "base"),
    "key-problem": ('extends = { name = "base", version = "1.0.0" }\nbogus = 1', "another catalog"),
    "not-a-table": ('extends = "base"', "another catalog"),
    "no-version": ('extends = { name = "base" }', "another catalog"),
}
EXTENDING = "error: blueprint.toml extends {name}, and this fastro can't merge an extending catalog with its base yet.\n"
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
        "rules": ["BP001", "BP002", "BP003", "BP004", "BP005", "BP006", "FR002", "FR014", "FR016"],
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


def test_a_wiring_path_under_another_is_a_bp001_finding(repo: Path) -> None:
    _edit(repo / "blueprint.toml", 'path = "backend/src/wiring/tasks.py"', 'path = "backend/src/wiring/app.py/tasks.py"')

    (finding,) = _findings()

    assert (finding["file"], finding["code"]) == ("blueprint.toml", "BP001")
    assert "'backend/src/wiring/app.py/tasks.py' sits under wiring[0]'s path" in str(finding["message"])


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


@pytest.mark.parametrize("flags", [[], ["--json"]])
@pytest.mark.parametrize("case", list(EXTENDS))
def test_an_extending_catalog_is_refused_and_exits_3_whatever_else_it_holds(repo: Path, case: str, flags: list[str]) -> None:
    extends, name = EXTENDS[case]
    _edit(repo / "blueprint.toml", 'requires_fastro = ">=0.1.0.dev0"', f'requires_fastro = ">=0.1.0.dev0"\n{extends}')
    _edit(repo / "fastro.toml", "source = true", "source = false")

    assert _lint(*flags) == (3, "", EXTENDING.format(name=name))


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

    found = {finding["code"]: finding["message"] for finding in _findings()}

    assert found["BP002"] == "features.cache.paths[0]: 'backend/src/modules/cache/' holds no tracked file."
    assert set(found) == {"BP002", "FR014"}


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


OVERLAY_KEY = 'requires_fastro = ">=0.1.0.dev0"\n'
OVERLAY_FIX = "Move or rename one of them, so that each path a project gets has one source."


def _overlay(repo: Path, *paths: str) -> list[tuple[object, object, object]]:
    _edit(repo / "blueprint.toml", OVERLAY_KEY, OVERLAY_KEY + 'overlay = "overlay"\nblueprint_only = ["docs/"]\n')
    _track(repo, *paths)
    return [(finding["line"], finding["message"], finding["fix"]) for finding in _findings() if finding["code"] == "BP003"]


def test_overlay_files_at_paths_no_project_gets_otherwise_are_fine(repo: Path) -> None:
    given = ("docs/guide.md", "overlay/README.md.j2", "overlay/docs/guide.md", "overlay/backend/new.py", "overlay/fastro.toml")

    assert _overlay(repo, *given) == []


@pytest.mark.parametrize(
    ("overlay", "other"),
    [
        ("overlay/backend/src/app.py.j2", "'backend/src/app.py'"),
        ("overlay/backend/src/modules/billing/routes.py", "'backend/src/modules/billing/routes.py'"),
        ("overlay/backend/src/modules/billing/paddle.py.j2", "'backend/src/modules/billing/paddle.py'"),
        ("overlay/frontend/menu.ts", "the wiring file 'frontend/menu.ts'"),
        ("overlay/backend/src/wiring/app.py.j2", "the wiring file 'backend/src/wiring/app.py'"),
    ],
    ids=["core", "feature", "when", "wiring-file", "wiring-file-rendered"],
)
def test_an_overlay_file_whose_project_path_another_source_gives_is_bp003_naming_both(
    repo: Path, overlay: str, other: str
) -> None:
    assert _overlay(repo, overlay) == [
        (
            _line_of(repo, 'overlay = "overlay"'),
            f"blueprint.overlay: {overlay!r} gives a project {other.split()[-1]}, which {other} gives it too.",
            OVERLAY_FIX,
        )
    ]


def test_an_overlay_file_at_a_root_file_core_gives_is_bp003(repo: Path) -> None:
    assert [message for _, message, _ in _overlay(repo, "README.md", "overlay/README.md.j2")] == [
        "blueprint.overlay: 'overlay/README.md.j2' gives a project 'README.md', which 'README.md' gives it too."
    ]


def test_two_overlay_files_with_one_project_path_are_bp003_naming_both(repo: Path) -> None:
    assert _overlay(repo, "overlay/README.md", "overlay/README.md.j2") == [
        (
            _line_of(repo, 'overlay = "overlay"'),
            "blueprint.overlay: 'overlay/README.md.j2' gives a project 'README.md', which 'overlay/README.md' gives it too.",
            OVERLAY_FIX,
        )
    ]


def test_a_wiring_file_no_selection_renders_and_that_isnt_tracked_still_collides(repo: Path) -> None:
    _git(repo, "rm", "-q", "--cached", "frontend/menu.ts")
    (repo / "frontend/menu.ts").unlink()

    assert [message for _, message, _ in _overlay(repo, "overlay/frontend/menu.ts.j2")] == [
        "blueprint.overlay: 'overlay/frontend/menu.ts.j2' gives a project 'frontend/menu.ts', which the wiring file "
        "'frontend/menu.ts' gives it too."
    ]


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


INSTALLED = 'module = "alembic.config"\n'
ADMIN = 'module = "src.modules.admin.site"\n'
TASKS = 'module = "src.modules.tasks.jobs"\n'
CLASH_FIX = "Rename one of the commands, so that no command's words begin another's."


def _clashes() -> list[tuple[object, object, object]]:
    return [(finding["code"], finding["message"], finding["fix"]) for finding in _findings()]


def test_a_command_whose_words_begin_another_s_is_bp006_at_that_command(repo: Path) -> None:
    _append(repo, f'[commands.db]\n{INSTALLED}[commands."db baseline"]\n{INSTALLED}[commands."db upgrade"]\n{INSTALLED}')

    code, _, _ = _lint()
    (finding,) = _findings()

    assert code == 1
    assert finding["line"] == 191
    assert (finding["code"], finding["message"], finding["fix"]) == (
        "BP006",
        "commands.db: 'db' begins the command 'db baseline', and fastro db can't be both a command and a group.",
        CLASH_FIX,
    )


def test_bp006_spans_the_top_level_and_features_whether_or_not_a_selection_holds_both(repo: Path) -> None:
    _edit(
        repo / "blueprint.toml", "[features.admin]\n", f'[features.admin.commands."users unlock"]\n{ADMIN}\n[features.admin]\n'
    )
    _edit(repo / "blueprint.toml", "[features.admin]\n", f"[features.tasks.commands.jobs]\n{TASKS}\n[features.admin]\n")
    _append(repo, f'[commands.users]\n{INSTALLED}[commands."jobs run"]\n{INSTALLED}')
    _edit(repo / "fastro.toml", ', "admin"', "")

    found = sorted(str(message) for _, message, _ in _clashes())

    assert found == [
        "commands.users: 'users' begins the command 'users unlock', and fastro users can't be both a command and a group.",
        "features.tasks.commands.jobs: 'jobs' begins the command 'jobs run', and fastro jobs can't be both a command and a "
        "group.",
    ]


def test_bp006_names_the_first_longer_command_in_declaration_order_and_reports_each_level(repo: Path) -> None:
    _edit(
        repo / "blueprint.toml",
        "[features.admin]\n",
        f'[features.admin.commands."users lock now"]\n{ADMIN}\n[features.admin]\n',
    )
    _append(repo, f'[commands."users unlock"]\n{INSTALLED}[commands.users]\n{INSTALLED}[commands."users lock"]\n{INSTALLED}')

    found = sorted(str(message) for code, message, _ in _clashes() if code == "BP006")

    assert found == [
        "commands.\"users lock\": 'users lock' begins the command 'users lock now', and fastro users lock can't be both a "
        "command and a group.",
        "commands.users: 'users' begins the command 'users lock now', and fastro users can't be both a command and a group.",
    ]


@pytest.mark.parametrize(
    ("params", "at", "message"),
    [
        (
            '[generators.deploy.params.dry_run]\ntype = "int"\ndefault = 1\n',
            "generators.deploy.params.dry_run",
            "takes the flag --dry-run, which is fastro make's own.",
        ),
        (
            '[generators.deploy.params.output_dir]\ntype = "str"\ndefault = ""\n',
            "generators.deploy.params.output_dir",
            "takes the flag --output-dir, which is fastro make's own.",
        ),
        (
            '[generators.deploy.params.cache]\ntype = "bool"\ndefault = true\n'
            '[generators.deploy.params.no_cache]\ntype = "str"\ndefault = ""\n',
            "generators.deploy.params.no_cache",
            "takes the flag --no-cache, which is param cache's.",
        ),
        (
            '[generators.deploy.params.no_cache]\ntype = "str"\ndefault = ""\n'
            '[generators.deploy.params.cache]\ntype = "bool"\ndefault = true\n',
            "generators.deploy.params.cache",
            "takes the flag --no-cache, which is param no_cache's.",
        ),
        (
            '[generators.deploy.params.help]\ntype = "bool"\ndefault = true\n',
            "generators.deploy.params.help",
            "takes the flag --help, which is fastro make's own.",
        ),
    ],
    ids=["own", "own-with-value", "no-flag", "no-flag-later", "bool-own"],
)
def test_a_generator_param_whose_flag_clashes_is_bp006_at_the_param(repo: Path, params: str, at: str, message: str) -> None:
    _append(repo, '[generators.deploy]\ntemplates = "blueprint/deploy"\n' + params)
    _track(repo, "blueprint/deploy/compose.yml")

    assert _clashes() == [("BP006", f"{at}: {message}", "Rename the param, so that its flags are its own.")]


def test_each_param_that_clashes_is_a_finding_once(repo: Path) -> None:
    _append(
        repo,
        '[generators.deploy]\ntemplates = "blueprint/deploy"\n'
        '[generators.deploy.params.no_force]\ntype = "str"\ndefault = ""\n'
        '[generators.deploy.params.force]\ntype = "bool"\ndefault = true\n'
        '[generators.deploy.params.yes]\ntype = "int"\ndefault = 1\n',
    )
    _track(repo, "blueprint/deploy/compose.yml")

    found = [str(message) for _, message, _ in _clashes()]

    assert found == [
        "generators.deploy.params.force: takes the flag --force, which is fastro make's own.",
        "generators.deploy.params.yes: takes the flag --yes, which is fastro make's own.",
    ]


@pytest.mark.parametrize("own", ["force", "help"])
def test_a_bool_param_that_clashes_with_make_s_own_flag_still_takes_its_no_flag(repo: Path, own: str) -> None:
    _append(
        repo,
        '[generators.deploy]\ntemplates = "blueprint/deploy"\n'
        f'[generators.deploy.params.{own}]\ntype = "bool"\ndefault = true\n'
        f'[generators.deploy.params.no_{own}]\ntype = "str"\ndefault = ""\n',
    )
    _track(repo, "blueprint/deploy/compose.yml")

    found = [str(message) for _, message, _ in _clashes()]

    assert found == [
        f"generators.deploy.params.{own}: takes the flag --{own}, which is fastro make's own.",
        f"generators.deploy.params.no_{own}: takes the flag --no-{own}, which is param {own}'s.",
    ]


def test_the_module_generator_s_params_arent_make_s_flags(repo: Path) -> None:
    _append(
        repo,
        '[generators.module]\ntemplates = "blueprint/module"\n[generators.module.params.dry_run]\ntype = "int"\ndefault = 1\n',
    )
    _track(repo, "blueprint/module/x.py")

    assert _clashes() == []


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


HEALTH = 'health_checks = ["src.health:database"]'
PROBE = "backend/src/probe.py"
REF_FIX = "Define it there, or point the ref at the module that does."


def _probe(repo: Path, code: str, ref: str = "src.probe:target") -> None:
    _edit(repo / "blueprint.toml", HEALTH, f'health_checks = ["src.health:database", "{ref}"]')
    (repo / PROBE).write_text(code)
    _git(repo, "add", PROBE)


@pytest.mark.parametrize(
    "code",
    [
        "def target(): ...\n",
        "async def target(): ...\n",
        "class target: ...\n",
        "target = 1\n",
        "target: int = 1\n",
        "target = 0\ntarget += 1\n",
        "target += 1\n",
        "first, target = 1, 2\n",
        "[first, *target] = [1, 2]\n",
        "other = target = 1\n",
        "import target\n",
        "import os as target\n",
        "import target.sub\n",
        "from os import path as target\n",
        "from os import target\n",
        "from os import *\n",
        "import sys\nif sys.platform:\n    target = 1\nelse:\n    pass\n",
        "import sys\nif sys.platform:\n    pass\nelse:\n    target = 1\n",
        "try:\n    target = 1\nexcept ImportError:\n    pass\n",
        "try:\n    import nothing\nexcept ImportError:\n    target = None\n",
        "try:\n    pass\nexcept ImportError:\n    pass\nelse:\n    target = 1\n",
        "try:\n    pass\nfinally:\n    target = 1\n",
        "with open(__file__) as handle:\n    target = handle.read()\n",
    ],
)
def test_a_ref_to_a_name_its_module_binds_at_module_level_is_fine(repo: Path, code: str) -> None:
    _probe(repo, code)

    assert _findings() == []


@pytest.mark.parametrize(
    "code",
    [
        "",
        "def other():\n    target = 1\n",
        "class Other:\n    target = 1\n",
        "for target in range(3):\n    pass\n",
        "print(target)\n",
        "obj = object()\nobj.target = 1\n",
        "import target_other\n",
    ],
)
def test_a_ref_to_a_name_its_module_doesnt_bind_at_module_level_is_fr014(repo: Path, code: str) -> None:
    _probe(repo, code)

    (finding,) = _findings()

    assert (finding["code"], finding["line"]) == ("FR014", 48)
    assert finding["message"] == (
        "core.contributes.health_checks[1]: 'src.probe:target' names 'target', which backend/src/probe.py doesn't "
        "define at module level."
    )
    assert finding["fix"] == REF_FIX


def test_a_package_s_init_is_the_module_it_checks(repo: Path) -> None:
    _probe(repo, "", "src.pkg:target")
    _track(repo, "backend/src/pkg/__init__.py")
    (repo / "backend/src/pkg/__init__.py").write_text("target = 1\n")

    assert _findings() == []


def test_a_ref_whose_module_has_no_tracked_file_is_fr014(repo: Path) -> None:
    _probe(repo, "", "src.missing:target")

    (finding,) = _findings()

    assert finding["code"] == "FR014"
    assert finding["message"] == (
        "core.contributes.health_checks[1]: 'src.missing:target' names a module the repo doesn't have: neither "
        "backend/src/missing.py nor backend/src/missing/__init__.py is tracked."
    )
    assert finding["fix"] == "Point the ref at a module the repo has, or add its file to git."


def test_a_ref_whose_module_doesnt_parse_is_fr014(repo: Path) -> None:
    _probe(repo, "def (\n")

    (finding,) = _findings()

    assert finding["code"] == "FR014"
    assert str(finding["message"]).startswith(
        "core.contributes.health_checks[1]: 'src.probe:target' can't be checked: backend/src/probe.py doesn't parse ("
    )
    assert str(finding["message"]).endswith(" on line 1).")


def test_an_aliased_ref_is_checked_by_its_attribute(repo: Path) -> None:
    _probe(repo, "target = 1\n", "src.probe:target as alias")

    assert _findings() == []


def test_a_ref_to_an_installed_package_isnt_checked(repo: Path) -> None:
    _probe(repo, "", "fastapi:nothing_here")

    assert _findings() == []


def test_a_feature_s_and_a_when_s_contributions_are_checked(repo: Path) -> None:
    _edit(
        repo / "blueprint.toml",
        'settings = ["src.modules.cache.settings:CacheSettings"]',
        'settings = ["src.modules.cache.settings:Nope"]',
    )

    (finding,) = _findings()

    assert str(finding["message"]).startswith("features.cache.contributes.settings[0]: 'src.modules.cache.settings:Nope'")

    _edit(
        repo / "blueprint.toml",
        'settings = ["src.modules.cache.settings:Nope"]',
        'settings = ["src.modules.cache.settings:CacheSettings"]',
    )
    _edit(repo / "blueprint.toml", "src.modules.accounts.email:send_queued", "src.modules.accounts.email:send_later")

    (finding,) = _findings()

    assert str(finding["message"]).startswith("features.accounts.when[0].contributes.email_sender[0]: ")


def test_a_reexport_fallback_is_checked(repo: Path) -> None:
    _edit(repo / "blueprint.toml", 'fallback = "src.admin_default:site"', 'fallback = "src.admin_default:nope"')

    (finding,) = _findings()

    assert str(finding["message"]).startswith("wiring[3].slots[0].fallback: 'src.admin_default:nope' names 'nope'")


def test_a_class_s_fallback_and_append_are_checked(repo: Path) -> None:
    _edit(
        repo / "blueprint.toml",
        'doc = "The app\'s settings."',
        'doc = "The app\'s settings."\nfallback = "src.mount:Base"\nappend = ["src.mount:Last"]',
    )

    found = sorted(str(finding["message"]).split(":")[0] for finding in _findings())

    assert found == ["wiring[0].slots[2].append[0]", "wiring[0].slots[2].fallback"]


def test_a_literal_or_data_slot_s_contributions_arent_refs(repo: Path) -> None:
    assert '"src.modules.tasks.jobs"' in (repo / "blueprint.toml").read_text()

    assert _findings() == []


def test_a_module_python_cant_place_an_error_in_has_no_line_in_its_finding(repo: Path) -> None:
    _probe(repo, "")
    (repo / PROBE).write_bytes(b"target = 1\x00\n")

    (finding,) = _findings()

    assert "line None" not in str(finding["message"])
    assert str(finding["message"]).endswith(
        "backend/src/probe.py doesn't parse (source code string cannot contain null bytes)."
    )


QUEUES = 'queues = ["tasks", "cache"]'


def test_a_verify_set_without_a_feature_a_listed_one_requires_is_bp004_at_that_entry(repo: Path) -> None:
    _edit(repo / "blueprint.toml", QUEUES, 'queues = ["tasks", "cache", "admin"]')

    (finding,) = _findings()

    assert (finding["code"], finding["line"]) == ("BP004", 12)
    assert finding["message"] == "verify.sets.queues[2]: lists 'admin', which requires 'accounts', and the set doesn't list it."
    assert finding["fix"] == "Add 'accounts' to the set."


def test_each_listed_feature_s_missing_requirement_is_its_own_finding(repo: Path) -> None:
    _edit(repo / "blueprint.toml", QUEUES, 'queues = ["billing", "admin"]')

    assert [str(finding["message"]).split(":")[0] for finding in _findings()] == [
        "verify.sets.queues[0]",
        "verify.sets.queues[1]",
    ]


def test_a_verify_set_closed_under_requires_is_fine(repo: Path) -> None:
    _edit(repo / "blueprint.toml", QUEUES, 'queues = ["billing", "admin", "accounts"]')

    assert _findings() == []


BACKEND = 'choices = ["memory", "redis"]\ndefault = "memory"\n'
ENV_EXAMPLE = "backend/.env.example"
MARKER = "# @fastro option=cache.backend\n"


def _env(repo: Path, example: str | None, env: str = 'env = { CACHE_BACKEND = "{value}" }\n') -> None:
    _edit(repo / "blueprint.toml", BACKEND, BACKEND + env)
    if example is not None:
        (repo / ENV_EXAMPLE).write_text(example)
        _git(repo, "add", ENV_EXAMPLE)


@pytest.mark.parametrize("example", [f"{MARKER}CACHE_BACKEND=memory\n", f"# Cache\nA=1\n\n{MARKER}CACHE_BACKEND=memory\nB=2\n"])
def test_an_option_env_var_with_one_marker_directly_above_it_is_fine(repo: Path, example: str) -> None:
    _env(repo, example)

    assert _findings() == []


@pytest.mark.parametrize(
    "example",
    [
        "CACHE_BACKEND=memory\n",
        f"{MARKER}\nCACHE_BACKEND=memory\n",
        f"{MARKER}# Options: memory, redis\nCACHE_BACKEND=memory\n",
        f"{MARKER}OTHER=1\nCACHE_BACKEND=memory\n",
        "# @fastro option=cache.other\nCACHE_BACKEND=memory\n",
        "CACHE_BACKEND=memory # @fastro option=cache.backend\n",
        "A=1 # @fastro option=cache.backend\nCACHE_BACKEND=memory\n",
        f"{MARKER}# CACHE_BACKEND=redis\n",
    ],
)
def test_an_option_env_var_without_a_marker_directly_above_it_is_fr016_at_the_var(repo: Path, example: str) -> None:
    _env(repo, example)

    (finding,) = _at_vars()

    assert (finding["file"], finding["code"]) == ("blueprint.toml", "FR016")
    assert finding["message"] == (
        "features.cache.options.backend.env.CACHE_BACKEND: has no '# @fastro option=cache.backend' marker in "
        "backend/.env.example."
    )
    assert finding["fix"] == (
        "Put '# @fastro option=cache.backend' on its own line, directly above CACHE_BACKEND= in backend/.env.example."
    )


def test_an_option_env_var_with_two_markers_is_fr016_naming_their_lines(repo: Path) -> None:
    _env(repo, f"{MARKER}CACHE_BACKEND=memory\n\n{MARKER}CACHE_BACKEND=redis\n")

    (finding,) = _findings()

    assert finding["code"] == "FR016"
    assert finding["message"] == (
        "features.cache.options.backend.env.CACHE_BACKEND: has 2 '# @fastro option=cache.backend' markers in "
        "backend/.env.example, on lines 1 and 4, and needs one."
    )
    assert finding["fix"] == "Keep one of them."


def test_each_var_of_an_option_s_env_needs_its_own_marker(repo: Path) -> None:
    _env(repo, f"{MARKER}CACHE_BACKEND=memory\n", 'env = { CACHE_BACKEND = "{value}", CACHE_URL = "{value}://" }\n')

    (finding,) = _findings()

    assert str(finding["message"]).startswith("features.cache.options.backend.env.CACHE_URL: has no ")


@pytest.mark.parametrize("tracked", [False, True])
def test_without_a_tracked_env_example_every_option_env_var_is_fr016(repo: Path, tracked: bool) -> None:
    _env(repo, None)
    if tracked:
        (repo / ENV_EXAMPLE).write_text(f"{MARKER}CACHE_BACKEND=memory\n")

    (finding,) = _findings()

    assert finding["code"] == "FR016"
    assert finding["message"] == (
        "features.cache.options.backend.env.CACHE_BACKEND: has no '# @fastro option=cache.backend' marker, since "
        "backend/.env.example isn't a tracked file."
    )


def _at_vars() -> list[dict[str, object]]:
    return [finding for finding in _findings() if str(finding["message"]).startswith("features.")]


def test_each_marker_line_6_1_refuses_is_fr016_at_blueprint_naming_its_line(repo: Path) -> None:
    _env(repo, f"{MARKER}CACHE_BACKEND=memory\n# @fastro iff=x\n{MARKER}OTHER=1\n# ===\n# @fastro if=cache\n")

    assert [(finding["line"], finding["code"], finding["message"], finding["fix"]) for finding in _findings()] == [
        (
            1,
            "FR016",
            f"blueprint: {ENV_EXAMPLE} line {number}: {reason}",
            f"Fix the marker on line {number} of {ENV_EXAMPLE}, or remove it.",
        )
        for number, reason in [
            (3, "'# @fastro iff=x' isn't a marker fastro knows: use '# @fastro if=' or '# @fastro option='."),
            (4, "OTHER isn't in the env table of cache.backend."),
            (7, "'# @fastro if=' sits on its own line directly below a section header, a block of '# ===' lines and a title."),
        ]
    ]


def test_an_option_without_env_needs_no_marker_and_no_env_example(repo: Path) -> None:
    assert not (repo / ENV_EXAMPLE).exists()

    assert _findings() == []


VERIFY = "[verify.sets]\n"


@pytest.mark.parametrize("tracked", ["backend/src/main.py", "backend/src/main/__init__.py"])
def test_a_verify_app_with_a_tracked_file_or_package_is_fine(repo: Path, tracked: str) -> None:
    _edit(repo / "blueprint.toml", VERIFY, '[verify]\napp = "src.main"\n\n[verify.sets]\n')
    _track(repo, tracked)

    assert _findings() == []


def test_a_verify_app_with_no_file_is_bp005_at_its_key(repo: Path) -> None:
    _edit(repo / "blueprint.toml", VERIFY, '[verify]\napp = "src.main"\n\n[verify.sets]\n')

    (finding,) = _findings()

    assert (finding["code"], finding["line"]) == ("BP005", 12)
    assert finding["message"] == (
        "verify.app: 'src.main' has no file: neither backend/src/main.py nor backend/src/main/__init__.py is tracked."
    )
    assert finding["fix"] == "Point app at a module the repo has, or add its file to git."


Replace = Callable[[Path, str, str], str]
READ_FIX = "Make it a regular file in the repo, or point at one that is."
NOT_READ = ["outside", "fifo", "dangling", "directory"]


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", NOT_READ)
@pytest.mark.parametrize("name", ["blueprint.toml", "fastro.toml"])
def test_a_config_file_fastro_may_not_read_exits_2_saying_why(repo: Path, replace: Replace, name: str, make: str) -> None:
    reason = replace(repo, name, make)

    code, out, err = _lint("--json")

    assert (code, out) == (2, "")
    assert f"error: {repo / name} {reason}. fastro reads {name} only as a regular file inside the repo." in err


def test_a_blueprint_toml_whose_symlink_leaves_the_repo_and_comes_back_is_read(repo: Path, replace: Replace) -> None:
    _edit(repo / "blueprint.toml", "schema = 1", 'schema = "one"')
    replace(repo, "blueprint.toml", "back")

    (finding,) = _findings()

    assert (finding["file"], finding["code"]) == ("blueprint.toml", "BP001")


def test_a_fastro_toml_whose_symlink_leaves_the_repo_and_comes_back_is_read(repo: Path, replace: Replace) -> None:
    _edit(repo / "fastro.toml", "source = true", "source = false")
    replace(repo, "fastro.toml", "back")

    (finding,) = _findings()

    assert (finding["file"], finding["code"]) == ("fastro.toml", "BP001")
    assert str(finding["message"]).startswith("source: ")


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", NOT_READ)
def test_a_ref_s_module_fastro_may_not_read_is_fr014_saying_why_and_isnt_read(repo: Path, replace: Replace, make: str) -> None:
    _probe(repo, "target = 1\n")
    reason = replace(repo, PROBE, make)

    (finding,) = _findings()

    assert (finding["code"], finding["line"]) == ("FR014", 48)
    assert finding["message"] == (
        f"core.contributes.health_checks[1]: 'src.probe:target' can't be checked: {PROBE} {reason}, and lint reads "
        "only regular files inside the repo."
    )
    assert finding["fix"] == READ_FIX


def test_a_ref_s_module_whose_symlink_leaves_the_repo_and_comes_back_is_read(repo: Path, replace: Replace) -> None:
    _probe(repo, "target = 1\n")
    replace(repo, PROBE, "back")

    assert _findings() == []


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", NOT_READ)
@pytest.mark.parametrize("unread", [PROBE, "backend/src/probe/__init__.py"])
def test_when_one_of_a_module_s_files_cant_be_read_fr014_reads_the_other(
    repo: Path, replace: Replace, unread: str, make: str
) -> None:
    _probe(repo, "target = 1\n")
    _track(repo, "backend/src/probe/__init__.py")
    (repo / "backend/src/probe/__init__.py").write_text("target = 1\n")
    replace(repo, unread, make)

    assert _findings() == []


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", NOT_READ)
def test_an_env_example_fastro_may_not_read_makes_every_option_env_var_fr016_saying_why(
    repo: Path, replace: Replace, make: str
) -> None:
    _env(repo, f"{MARKER}CACHE_BACKEND=memory\n")
    reason = replace(repo, ENV_EXAMPLE, make)

    (finding,) = _findings()

    assert finding["code"] == "FR016"
    assert finding["message"] == (
        "features.cache.options.backend.env.CACHE_BACKEND: has no '# @fastro option=cache.backend' marker, since "
        f"{ENV_EXAMPLE} {reason}, and lint reads only regular files inside the repo."
    )
    assert finding["fix"] == f"Make {ENV_EXAMPLE} a regular file in the repo."


def test_an_env_example_that_isnt_utf_8_makes_every_option_env_var_fr016_saying_so(repo: Path) -> None:
    _env(repo, f"{MARKER}CACHE_BACKEND=memory\n")
    (repo / ENV_EXAMPLE).write_bytes(f"# caf\xe9\n{MARKER}CACHE_BACKEND=memory\n".encode("latin-1"))

    (finding,) = _findings()

    assert finding["code"] == "FR016"
    assert finding["message"] == (
        "features.cache.options.backend.env.CACHE_BACKEND: has no '# @fastro option=cache.backend' marker, since "
        f"{ENV_EXAMPLE} isn't UTF-8 text."
    )
    assert finding["fix"] == f"Save {ENV_EXAMPLE} as UTF-8."


@pytest.mark.parametrize("separator", ["\r", "\f", "\u2028"])
def test_an_env_example_splits_into_lines_at_newline_only(repo: Path, separator: str) -> None:
    _env(repo, f"# @fastro option=cache.backend{separator}CACHE_BACKEND=memory\n")

    (finding,) = _at_vars()

    assert finding["code"] == "FR016"
    assert finding["message"] == (
        "features.cache.options.backend.env.CACHE_BACKEND: has no '# @fastro option=cache.backend' marker in "
        "backend/.env.example."
    )


def test_an_env_example_is_read_past_a_byte_order_mark(repo: Path) -> None:
    _env(repo, f"\ufeff{MARKER}CACHE_BACKEND=memory\n")

    assert _findings() == []


def test_an_env_example_whose_symlink_leaves_the_repo_and_comes_back_is_read(repo: Path, replace: Replace) -> None:
    _env(repo, f"{MARKER}CACHE_BACKEND=memory\n")
    replace(repo, ENV_EXAMPLE, "back")

    assert _findings() == []


def test_when_neither_of_a_module_s_files_can_be_read_fr014_names_the_py_file(repo: Path, replace: Replace) -> None:
    _probe(repo, "target = 1\n")
    _track(repo, "backend/src/probe/__init__.py")
    replace(repo, PROBE, "outside")
    replace(repo, "backend/src/probe/__init__.py", "directory")

    (finding,) = _findings()

    assert f"can't be checked: {PROBE} leads outside the repo," in str(finding["message"])


def test_a_refused_fastro_toml_is_reported_before_anything_else_is_read(repo: Path, replace: Replace) -> None:
    shutil.rmtree(repo / ".git")
    reason = replace(repo, "fastro.toml", "dangling")

    code, _, err = _lint()

    assert code == 2
    assert f"error: {repo / 'fastro.toml'} {reason}." in err


@pytest.mark.parametrize("name", ["blueprint.toml", "fastro.toml"])
def test_a_config_file_fastro_isnt_allowed_to_resolve_exits_3(repo: Path, name: str) -> None:
    locked = repo.parent / "locked"
    locked.mkdir()
    (locked / name).write_bytes((repo / name).read_bytes())
    (repo / name).unlink()
    (repo / name).symlink_to(locked / name)
    locked.chmod(0)
    try:
        code, out, err = _lint("--json")
    finally:
        locked.chmod(0o755)

    assert (code, out) == (3, "")
    assert f"error: can't read {repo / name}: Permission denied." in err


def test_a_blueprint_toml_fastro_cant_tell_is_there_is_unreadable(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(linting.Unreadable, match=f"can't read {locked / 'blueprint.toml'}: Permission denied"):
            linting.lint(locked)
    finally:
        locked.chmod(0o755)


def test_a_module_fastro_cant_check_is_fr014_saying_why_not_untracked(repo: Path) -> None:
    _edit(repo / "blueprint.toml", HEALTH, 'health_checks = ["src.health:database", "src.locked.mod:target"]')
    _track(repo, "backend/src/locked/mod.py")
    (repo / "backend/src/locked").chmod(0)
    try:
        (finding,) = _findings()
    finally:
        (repo / "backend/src/locked").chmod(0o755)

    assert finding["code"] == "FR014"
    assert finding["message"] == (
        "core.contributes.health_checks[1]: 'src.locked.mod:target' can't be checked: backend/src/locked/mod.py "
        "can't be resolved: Permission denied, and lint reads only regular files inside the repo."
    )


def test_a_ref_s_module_file_the_system_wont_let_lint_open_exits_3(repo: Path) -> None:
    _probe(repo, "target = 1\n")
    (repo / PROBE).chmod(0)
    try:
        code, out, err = _lint("--json")
    finally:
        (repo / PROBE).chmod(0o644)

    assert (code, out) == (3, "")
    assert f"error: can't read {repo / PROBE}: Permission denied." in err


def test_an_env_example_the_system_wont_let_lint_open_exits_3(repo: Path) -> None:
    _env(repo, f"{MARKER}CACHE_BACKEND=memory\n")
    (repo / ENV_EXAMPLE).chmod(0)
    try:
        code, out, err = _lint("--json")
    finally:
        (repo / ENV_EXAMPLE).chmod(0o644)

    assert (code, out) == (3, "")
    assert f"error: can't read {repo / ENV_EXAMPLE}: Permission denied." in err


def test_each_mode_of_a_generator_is_a_directory_that_must_hold_a_tracked_file(repo: Path) -> None:
    _append(repo, '[generators.deploy]\ntemplates = "blueprint/deploy"\nmodes = ["local", "prod"]\n')
    _track(repo, "blueprint/deploy/local/compose.yml.j2", "blueprint/deploy/README.md")

    found = [str(finding["message"]) for finding in _findings()]

    assert found == ["generators.deploy.modes[1]: 'blueprint/deploy/prod/' holds no tracked file."]

    _track(repo, "blueprint/deploy/prod/compose.yml.j2")

    assert _findings() == []


PYPROJECT = "backend/pyproject.toml"
NAME_FIX = f'Set name under [project] in {PYPROJECT} to a valid PEP 508 name, such as name = "my-api".'
VERSION_FIX = f'Set version = "0.1.0" under [project] in {PYPROJECT}, or change [blueprint].version to match it.'


def _pyproject(repo: Path, text: str | bytes) -> None:
    file = repo / PYPROJECT
    if isinstance(text, str):
        file.write_text(text)
    else:
        file.write_bytes(text)


def _fr002() -> list[tuple[object, object, object, object]]:
    return [
        (finding["line"], finding["message"], finding["fix"], finding["file"])
        for finding in _findings()
        if finding["code"] == "FR002"
    ]


@pytest.mark.parametrize("version", ["0.1.0", "0.1", "0.1.0.0", "v0.1.0"])
def test_a_pyproject_version_equal_to_the_blueprint_s_under_pep_440_is_fine(repo: Path, version: str) -> None:
    _pyproject(repo, f'[project]\nname = "mini-backend"\nversion = "{version}"\n')

    assert _findings() == []


def test_a_pyproject_read_past_a_byte_order_mark_is_fine(repo: Path) -> None:
    _pyproject(repo, b'\xef\xbb\xbf[project]\nname = "mini-backend"\nversion = "0.1.0"\n')

    assert _findings() == []


def test_a_pyproject_version_that_differs_is_fr002_at_the_blueprint_version(repo: Path) -> None:
    _pyproject(repo, '[project]\nname = "mini-backend"\nversion = "0.2.0"\n')

    code, _, _ = _lint()

    assert code == 1
    assert _fr002() == [
        (
            3,
            f"blueprint.version: is 0.1.0, and {PYPROJECT}'s [project].version is '0.2.0'.",
            VERSION_FIX,
            "blueprint.toml",
        )
    ]


@pytest.mark.parametrize("version", ['version = "lots"', "version = 1", ""])
def test_a_pyproject_without_a_pep_440_version_string_is_fr002(repo: Path, version: str) -> None:
    _pyproject(repo, f'[project]\nname = "mini-backend"\n{version}\n')

    assert _fr002() == [
        (
            3,
            f"blueprint.version: can't be compared, since {PYPROJECT} has no [project].version string that's a PEP 440 "
            "version.",
            VERSION_FIX,
            "blueprint.toml",
        )
    ]


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("", "has no [project].name string"),
        ("name = 3", "has no [project].name string"),
        ('name = "-mini"', "has [project].name '-mini', which isn't a valid name"),
        ('name = "mini backend"', "has [project].name 'mini backend', which isn't a valid name"),
    ],
)
def test_a_pyproject_without_a_valid_name_is_fr002_saying_so(repo: Path, name: str, said: str) -> None:
    _pyproject(repo, f'[project]\n{name}\nversion = "0.1.0"\n')

    assert _fr002() == [
        (
            3,
            f"blueprint.version: {PYPROJECT} {said}: use letters, digits, '.', '_' and '-', starting and ending with a "
            "letter or digit, as PEP 508 says.",
            NAME_FIX,
            "blueprint.toml",
        )
    ]


@pytest.mark.parametrize("text", ['title = "no project table"\n', 'project = "mini-backend"\n'])
def test_a_version_problem_and_a_name_problem_are_a_finding_each(repo: Path, text: str) -> None:
    _pyproject(repo, text)

    found = [str(message) for _, message, _, _ in _fr002()]

    assert found == [
        f"blueprint.version: {PYPROJECT} has no [project].name string: use letters, digits, '.', '_' and '-', starting "
        "and ending with a letter or digit, as PEP 508 says.",
        f"blueprint.version: can't be compared, since {PYPROJECT} has no [project].version string that's a PEP 440 version.",
    ]


@pytest.mark.parametrize(
    ("make", "said", "fix"),
    [
        ("untracked", "isn't a tracked file", f"Add {PYPROJECT} to git."),
        ("not-utf-8", "isn't UTF-8 text", f"Save {PYPROJECT} as UTF-8."),
        ("not-toml", "isn't valid TOML", f"Fix {PYPROJECT} so it parses."),
    ],
)
def test_a_pyproject_lint_cant_read_is_one_fr002_finding_saying_why(repo: Path, make: str, said: str, fix: str) -> None:
    if make == "untracked":
        _git(repo, "rm", "-q", "--cached", PYPROJECT)
    if make == "not-utf-8":
        _pyproject(repo, b'[project]\nname = "mini\xff"\nversion = "0.1.0"\n')
    if make == "not-toml":
        _pyproject(repo, "[project\n")

    ((line, message, found_fix, file),) = _fr002()

    assert (line, file, found_fix) == (3, "blueprint.toml", fix)
    assert str(message).startswith(f"blueprint.version: can't be checked against {PYPROJECT}, since it {said}")


@pytest.mark.usefixtures("hang_guard")
@pytest.mark.parametrize("make", NOT_READ)
def test_a_pyproject_fastro_may_not_read_is_one_fr002_finding_saying_why(repo: Path, replace: Replace, make: str) -> None:
    reason = replace(repo, PYPROJECT, make)

    assert _fr002() == [
        (
            3,
            f"blueprint.version: can't be checked against {PYPROJECT}, since it {reason}, and lint reads only regular "
            "files inside the repo.",
            f"Make {PYPROJECT} a regular file in the repo.",
            "blueprint.toml",
        )
    ]


def test_a_pyproject_the_system_wont_let_lint_open_exits_3(repo: Path) -> None:
    (repo / PYPROJECT).chmod(0)
    try:
        code, out, err = _lint("--json")
    finally:
        (repo / PYPROJECT).chmod(0o644)

    assert (code, out) == (3, "")
    assert f"error: can't read {repo / PYPROJECT}: Permission denied." in err


def test_a_pyproject_lint_isnt_allowed_to_resolve_is_an_fr002_finding_saying_why(repo: Path) -> None:
    (repo / "backend").chmod(0o600)
    try:
        code, out, _ = _lint("--json")
    finally:
        (repo / "backend").chmod(0o755)

    found = [finding for finding in json.loads(out)["findings"] if finding["code"] == "FR002"]
    assert code == 1
    assert [finding["message"] for finding in found] == [
        f"blueprint.version: can't be checked against {PYPROJECT}, since it can't be resolved: Permission denied, and "
        "lint reads only regular files inside the repo."
    ]


DEV_PYPROJECT = (
    '[project]\nname = "mini-backend"\nversion = "0.1.0"\n\n'
    '[project.optional-dependencies]\ndev = ["pytest", "Fake_Redis>=2"]\n\n'
    '[dependency-groups]\ndev = ["freezegun", { include-group = "lint" }, "not a requirement ~"]\nlint = ["ruff"]\n'
)
CACHE_SUMMARY = 'summary = "A cache for slow reads"\n'
DEV_FIX = (
    "Add {text!r} to [project.optional-dependencies].dev or [dependency-groups].dev in " + PYPROJECT + ", or remove it here."
)
MERGE_FIX = (
    "Give every entry for {name} the same environment marker, and the same direct reference when one is a direct "
    "reference, or drop all but one."
)


def _line_of(repo: Path, text: str) -> int:
    lines = (repo / "blueprint.toml").read_text().splitlines()
    return next(number for number, line in enumerate(lines, 1) if text in line)


def test_dev_dependencies_every_dev_table_lists_by_normalized_name_are_fine(repo: Path) -> None:
    _pyproject(repo, DEV_PYPROJECT)
    _edit(repo / "blueprint.toml", "[presets]\n", '[blueprint.dependencies]\ndev = ["pytest>=8"]\n\n[presets]\n')
    _edit(repo / "blueprint.toml", CACHE_SUMMARY, CACHE_SUMMARY + 'dev_dependencies = ["fake.redis[lua]", "FreezeGun"]\n')

    assert _findings() == []


def test_a_dev_dependency_no_dev_table_lists_is_fr002_at_its_entry(repo: Path) -> None:
    _pyproject(repo, DEV_PYPROJECT)
    toml = repo / "blueprint.toml"
    _edit(toml, "[presets]\n", '[blueprint.dependencies]\ndev = ["mypy"]\n\n[presets]\n')
    _edit(toml, CACHE_SUMMARY, CACHE_SUMMARY + 'dev_dependencies = ["pytest", "respx>=0.20"]\n')
    _edit(toml, 'services = ["redis"]\n', 'services = ["redis"]\ndev_dependencies = ["ruff"]\n')

    assert _fr002() == [
        (
            _line_of(repo, 'dev = ["mypy"]'),
            f"blueprint.dependencies.dev[0]: 'mypy' isn't listed in a dev table of {PYPROJECT}, so no project gets it.",
            DEV_FIX.format(text="mypy"),
            "blueprint.toml",
        ),
        (
            _line_of(repo, "respx"),
            f"features.cache.dev_dependencies[1]: 'respx>=0.20' isn't listed in a dev table of {PYPROJECT}, so no "
            "project gets it.",
            DEV_FIX.format(text="respx>=0.20"),
            "blueprint.toml",
        ),
        (
            _line_of(repo, 'dev_dependencies = ["ruff"]'),
            f"features.cache.when[0].dev_dependencies[0]: 'ruff' isn't listed in a dev table of {PYPROJECT}, so no "
            "project gets it.",
            DEV_FIX.format(text="ruff"),
            "blueprint.toml",
        ),
    ]


def test_without_dev_tables_every_declared_dev_dependency_is_fr002(repo: Path) -> None:
    _edit(repo / "blueprint.toml", CACHE_SUMMARY, CACHE_SUMMARY + 'dev_dependencies = ["pytest"]\n')

    assert [message for _, message, _, _ in _fr002()] == [
        f"features.cache.dev_dependencies[0]: 'pytest' isn't listed in a dev table of {PYPROJECT}, so no project gets it."
    ]


def test_a_pyproject_lint_cant_read_is_the_one_finding_for_the_dev_dependencies_too(repo: Path) -> None:
    _pyproject(repo, "[project\n")
    _edit(repo / "blueprint.toml", CACHE_SUMMARY, CACHE_SUMMARY + 'dev_dependencies = ["pytest"]\n')

    assert [line for line, _, _, _ in _fr002()] == [3]


def _runtime(repo: Path, core: str, cache: str = "", backend: str = "", billing: str = "", provider: str = "") -> None:
    toml = repo / "blueprint.toml"
    _edit(toml, "[presets]\n", f"[blueprint.dependencies]\nruntime = {core}\n\n[presets]\n")
    _edit(toml, CACHE_SUMMARY, CACHE_SUMMARY + cache + "\n")
    _edit(toml, 'default = "memory"\n', f'default = "memory"\n{backend}\n')
    _edit(toml, 'summary = "Payments"\n', f'summary = "Payments"\n{billing}\n')
    _edit(toml, 'default = "stripe"\n', f'default = "stripe"\n{provider}\n')


def test_runtime_entries_with_different_environment_markers_are_fr002_at_the_later_naming_the_first(repo: Path) -> None:
    _runtime(
        repo,
        """["uvloop; sys_platform != 'win32'"]""",
        cache="""dependencies = ["UVLoop>=0.19; sys_platform == 'linux'"]""",
        billing='dependencies = ["uvloop"]',
    )
    first = _line_of(repo, "runtime = ")

    assert _fr002() == [
        (
            _line_of(repo, 'dependencies = ["uvloop"]'),
            f"""features.billing.dependencies[0]: 'uvloop' can't be merged with "uvloop; sys_platform != 'win32'" on """
            f"line {first}, since uvloop has entries with different environment markers.",
            MERGE_FIX.format(name="uvloop"),
            "blueprint.toml",
        ),
        (
            _line_of(repo, "UVLoop"),
            f"""features.cache.dependencies[0]: "UVLoop>=0.19; sys_platform == 'linux'" can't be merged with """
            f""""uvloop; sys_platform != 'win32'" on line {first}, since uvloop has entries with different """
            "environment markers.",
            MERGE_FIX.format(name="uvloop"),
            "blueprint.toml",
        ),
    ]


def test_the_order_is_the_file_s_so_core_written_last_gets_the_finding(repo: Path) -> None:
    toml = repo / "blueprint.toml"
    _edit(toml, CACHE_SUMMARY, CACHE_SUMMARY + """dependencies = ["uvloop; os_name == 'nt'"]\n""")
    toml.write_text(toml.read_text() + '\n[blueprint.dependencies]\nruntime = ["uvloop"]\n')

    assert [message for _, message, _, _ in _fr002()] == [
        "blueprint.dependencies.runtime[0]: 'uvloop' can't be merged with \"uvloop; os_name == 'nt'\" on line "
        f"{_line_of(repo, 'os_name')}, since uvloop has entries with different environment markers."
    ]


def test_entries_6_3_merges_are_fine_and_different_values_of_one_option_are_never_compared(repo: Path) -> None:
    _runtime(
        repo,
        '["redis>=4", "pkg @ https://example.com/pkg-1.0.tar.gz"]',
        cache='dependencies = ["Redis[hiredis]<6", "pkg@https://example.com/pkg-1.0.tar.gz"]',
        backend="""dependencies = { memory = ["uvloop; os_name == 'nt'"], redis = ["uvloop"] }""",
    )

    assert _fr002() == []


def test_a_direct_reference_that_isnt_the_same_is_fr002_and_a_when_s_if_isnt_considered(repo: Path) -> None:
    _runtime(repo, '["pkg @ https://example.com/pkg-1.0.tar.gz"]')
    _edit(repo / "blueprint.toml", 'services = ["redis"]\n', 'services = ["redis"]\ndependencies = ["pkg>=1"]\n')

    assert [message for _, message, _, _ in _fr002()] == [
        "features.cache.when[0].dependencies[0]: 'pkg>=1' can't be merged with 'pkg @ https://example.com/pkg-1.0.tar.gz' "
        f"on line {_line_of(repo, 'runtime = ')}, since pkg has a direct reference, and its entries aren't the same "
        "requirement."
    ]


def test_options_of_two_features_are_compared(repo: Path) -> None:
    _runtime(
        repo,
        "[]",
        backend='dependencies = { redis = ["uvloop"] }',
        provider="""dependencies = { paddle = ["uvloop; os_name == 'nt'"] }""",
    )

    assert [line for line, _, _, _ in _fr002()] == [_line_of(repo, 'redis = ["uvloop"]')]


def test_runtime_clashes_are_found_when_the_pyproject_cant_be_read(repo: Path) -> None:
    _pyproject(repo, "[project\n")
    _runtime(repo, '["uvloop"]', cache="""dependencies = ["uvloop; os_name == 'nt'"]""")

    assert [line for line, _, _, _ in _fr002()] == [3, _line_of(repo, "os_name")]


def test_two_entries_of_one_option_value_are_compared(repo: Path) -> None:
    _runtime(repo, "[]", backend="""dependencies = { redis = ["uvloop", "uvloop; os_name == 'nt'"] }""")

    assert [message for _, message, _, _ in _fr002()] == [
        "features.cache.options.backend.dependencies.redis[1]: \"uvloop; os_name == 'nt'\" can't be merged with "
        f"'uvloop' on line {_line_of(repo, 'redis = [')}, since uvloop has entries with different environment markers."
    ]
