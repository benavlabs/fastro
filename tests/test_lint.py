"""``fastro blueprint lint``: a blueprint repo's load problems as findings, BP001 and BP003 (spec 2.13, 7, 8)."""

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
        "rules": ["BP001", "BP002", "BP003", "BP004", "BP005", "FR014", "FR016"],
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

    (finding,) = _findings()

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
