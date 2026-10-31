"""Planning what a project gets from its blueprint (spec 7, rev 1.17): one op per path, each with its origin and reason."""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from packaging.version import Version

from fastro.engine import blueprint, lock
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import Op, plan
from fastro.engine.resolve import select
from fastro.engine.spec import RepoPath

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
SHA = "ab" * 32


@pytest.fixture(scope="module")
def mini() -> Blueprint:
    return blueprint.load("blueprint.toml", (MINI / "blueprint.toml").read_text(), Version("0.1.0"))


@pytest.fixture(scope="module")
def files() -> tuple[RepoPath, ...]:
    return tuple(RepoPath(str(path.relative_to(MINI))) for path in sorted(MINI.rglob("*")) if path.is_file())


def _ops(
    catalog: Blueprint,
    files: tuple[RepoPath, ...],
    features: tuple[str, ...] = (),
    options: Mapping[tuple[str, str], str] | None = None,
    app_paths: tuple[RepoPath, ...] = (),
    previous: lock.Lock | None = None,
) -> dict[str, Op]:
    selection = select(catalog, features, options=options)
    return {op.path: op for op in plan(catalog, selection, files, app_paths, previous).ops}


def test_the_minimal_preset_copies_accounts_and_core_and_skips_the_rest(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    ops = _ops(mini, files, features=mini.presets["minimal"])

    assert ops["backend/src/modules/accounts/routes.py"] == Op(
        "copy", "backend/src/modules/accounts/routes.py", "feature:accounts", "feature accounts is selected"
    )
    assert ops["backend/src/app.py"] == Op("copy", "backend/src/app.py", "core", "no feature owns it")
    assert ops["backend/src/modules/billing/routes.py"] == Op(
        "skip", "backend/src/modules/billing/routes.py", "feature:billing", "feature billing isn't selected"
    )
    assert ops["backend/src/modules/billing/paddle.py"] == Op(
        "skip", "backend/src/modules/billing/paddle.py", "feature:billing", "feature billing isn't selected"
    )
    assert ops["blueprint.toml"] == Op("skip", "blueprint.toml", "core", "it's blueprint_only")


def test_wiring_is_rendered_and_a_wiring_file_whose_if_fails_is_skipped(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    ops = _ops(mini, files, features=mini.presets["minimal"])

    assert ops["backend/src/wiring/app.py"] == Op("render", "backend/src/wiring/app.py", "wiring", "generated wiring")
    assert ops["frontend/menu.ts"] == Op("skip", "frontend/menu.ts", "wiring", "the wiring file's if, admin, doesn't hold")


def test_a_wiring_file_whose_if_holds_says_so(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    ops = _ops(mini, files, features=mini.presets["full"])

    assert ops["frontend/menu.ts"] == Op("render", "frontend/menu.ts", "wiring", "generated wiring, since admin holds")


def test_a_wiring_file_whose_if_fails_and_isnt_in_the_source_gets_no_op(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    ops = _ops(mini, tuple(path for path in files if path.text != "frontend/menu.ts"), features=mini.presets["minimal"])

    assert "frontend/menu.ts" not in ops


@pytest.mark.parametrize(
    ("provider", "op", "reason"),
    [
        ("stripe", "skip", "billing.provider=paddle doesn't hold"),
        ("paddle", "copy", "feature billing is selected and billing.provider=paddle holds"),
    ],
)
def test_a_when_path_follows_its_condition(
    mini: Blueprint, files: tuple[RepoPath, ...], provider: str, op: str, reason: str
) -> None:
    ops = _ops(mini, files, features=("billing",), options={("billing", "provider"): provider})

    assert ops["backend/src/modules/billing/paddle.py"] == Op(
        op, "backend/src/modules/billing/paddle.py", "feature:billing", reason
    )


def test_a_file_owned_by_app_is_skipped_as_the_projects_own_code(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    ops = _ops(mini, files, features=mini.presets["full"], app_paths=(RepoPath("backend/src/modules/billing/routes.py"),))

    assert ops["backend/src/modules/billing/routes.py"] == Op(
        "skip", "backend/src/modules/billing/routes.py", "app", "it's the project's own code"
    )


def test_blueprint_only_comes_before_every_other_skip_cause(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    text = (
        (MINI / "blueprint.toml")
        .read_text()
        .replace(
            'requires_fastro = ">=0.1.0.dev0"\n',
            'requires_fastro = ">=0.1.0.dev0"\nblueprint_only = ["backend/src/modules/billing/"]\n',
        )
    )
    catalog = blueprint.load("blueprint.toml", text, Version("0.1.0"))
    ops = _ops(catalog, files, app_paths=(RepoPath("backend/src/modules/billing/routes.py"),))

    assert ops["backend/src/modules/billing/routes.py"].reason == "it's blueprint_only"
    assert ops["backend/src/modules/billing/routes.py"].origin == "app"


def _previous(*entries: tuple[str, str]) -> lock.Lock:
    rows = "".join(f'"{path}" = {{ sha256 = "{SHA}", origin = "{origin}" }}\n' for path, origin in entries)
    return lock.load(
        "lock.toml",
        '[lock]\nschema = 1\nfastro = "0.1.0"\nblueprint = { name = "mini", version = "0.1.0", source = "../mini" }\n'
        f"features = []\n[files]\n{rows}",
    )


def test_a_file_the_previous_lock_has_and_this_plan_doesnt_copy_is_deleted(
    mini: Blueprint, files: tuple[RepoPath, ...]
) -> None:
    previous = _previous(
        ("backend/src/modules/billing/routes.py", "feature:billing"),
        ("backend/src/modules/gone.py", "feature:billing"),
        ("backend/src/modules/accounts/routes.py", "feature:accounts"),
    )
    ops = _ops(mini, files, features=mini.presets["minimal"], previous=previous)

    deleted = "in the previous lock, and not in this plan"
    assert ops["backend/src/modules/billing/routes.py"] == Op(
        "delete", "backend/src/modules/billing/routes.py", "feature:billing", deleted
    )
    assert ops["backend/src/modules/gone.py"] == Op("delete", "backend/src/modules/gone.py", "feature:billing", deleted)
    assert ops["backend/src/modules/accounts/routes.py"].op == "copy"


@pytest.mark.parametrize("origin", ["deps", "env", "overlay", "make:module", "plugin:stripe"])
def test_a_locked_file_from_a_step_this_plan_doesnt_cover_gets_no_op(
    mini: Blueprint, files: tuple[RepoPath, ...], origin: str
) -> None:
    ops = _ops(mini, files, previous=_previous(("backend/src/elsewhere.py", origin)))

    assert "backend/src/elsewhere.py" not in ops


def test_ops_are_one_per_path_and_sorted_by_path(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    result = plan(mini, select(mini, mini.presets["full"]), files)
    paths = [op.path for op in result.ops]

    assert paths == sorted(paths)
    assert len(paths) == len(set(paths))
    assert set(paths) == {path.text for path in files}


def test_the_plan_serializes_as_spec_7s_document_with_the_unfilled_parts_empty(
    mini: Blueprint, files: tuple[RepoPath, ...]
) -> None:
    selection = select(mini, ("billing",), options={("billing", "provider"): "paddle"})
    document = plan(mini, selection, files).document()

    assert list(document) == ["schema", "blueprint", "selection", "ops", "dependencies", "migrations", "next"]
    assert document["schema"] == 1
    assert document["blueprint"] == {"name": "mini", "version": "0.1.0"}
    assert document["selection"] == {
        "features": ["accounts", "billing"],
        "options": {"billing.provider": "paddle"},
        "plugins": [],
        "modules": [],
    }
    assert (document["dependencies"], document["migrations"], document["next"]) == (
        {"add": [], "remove": []},
        {"drops": [], "adds": []},
        [],
    )
    assert document["ops"][0] == {
        "op": "copy",
        "path": "backend/pyproject.toml",
        "origin": "core",
        "reason": "no feature owns it",
    }
    assert json.loads(json.dumps(document)) == document


def test_a_rendered_wiring_file_whose_if_now_fails_is_deleted(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    ops = _ops(mini, files, features=mini.presets["minimal"], previous=_previous(("frontend/menu.ts", "wiring")))

    assert ops["frontend/menu.ts"] == Op("delete", "frontend/menu.ts", "wiring", "in the previous lock, and not in this plan")


def test_a_core_file_gone_from_the_source_is_deleted(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    ops = _ops(mini, files, previous=_previous(("backend/src/removed.py", "core")))

    assert ops["backend/src/removed.py"] == Op(
        "delete", "backend/src/removed.py", "core", "in the previous lock, and not in this plan"
    )


def test_a_locked_wiring_file_that_is_rendered_again_is_kept(mini: Blueprint, files: tuple[RepoPath, ...]) -> None:
    ops = _ops(mini, files, previous=_previous(("backend/src/wiring/app.py", "wiring")))

    assert ops["backend/src/wiring/app.py"] == Op("render", "backend/src/wiring/app.py", "wiring", "generated wiring")


@pytest.mark.parametrize("path", ["blueprint.toml", "fastro.toml", ".fastro/lock.toml", ".fastro/blueprint.toml"])
def test_the_files_every_blueprint_keeps_to_itself_are_skipped_without_being_listed(
    mini: Blueprint, files: tuple[RepoPath, ...], path: str
) -> None:
    ops = _ops(mini, (*files, RepoPath(".fastro/lock.toml"), RepoPath(".fastro/blueprint.toml")))

    assert mini.info.blueprint_only == ()
    assert ops[path] == Op("skip", path, "core", "it's blueprint_only")
