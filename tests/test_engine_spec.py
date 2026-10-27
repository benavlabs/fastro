"""The definitions every catalog file shares: names, paths, refs and versions (spec section 1)."""

import re

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from fastro.engine.conditions import Option, parse
from fastro.engine.spec import (
    Ref,
    RepoPath,
    SpecError,
    allows,
    blueprint_name,
    choice,
    feature_name,
    label,
    option_name,
    repo_path,
    requirement,
    specifier,
    tool_allowed,
    version,
)


@pytest.mark.parametrize("name", ["accounts", "tier_limits", "a", "api_keys2"])
def test_a_feature_name_is_lowercase_letters_digits_and_underscores(name: str) -> None:
    assert feature_name(name) == name
    assert option_name(name) == name


@pytest.mark.parametrize("name", ["Accounts", "2fa", "_private", "api-keys", "tier.limits", "", "café"])
def test_any_other_feature_name_is_refused(name: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{name!r} isn't a valid name")):
        feature_name(name)
    with pytest.raises(SpecError, match=re.escape(f"{name!r} isn't a valid name")):
        option_name(name)


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("app", "the project's own code"),
        ("core", "the files no feature owns"),
        ("and", "the condition language"),
        ("or", "the condition language"),
        ("not", "the condition language"),
    ],
)
def test_reserved_names_are_refused_for_features_but_not_options(name: str, why: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"'{name}' is reserved for {why}")):
        feature_name(name)
    assert option_name(name) == name


@pytest.mark.parametrize("text", ["redis", "rabbitmq", "prod-eu.2", "v1:2", "Redis"])
def test_a_choice_is_any_run_of_characters_a_condition_can_name(text: str) -> None:
    assert choice(text) == text
    assert parse(f"taskiq.broker={text}") == Option("taskiq", "broker", text)


@pytest.mark.parametrize("text", ["", "two words", "tab\there", "a(b", "a)b", "a=b", "redis "])
def test_a_choice_a_condition_couldnt_name_is_refused(text: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{text!r} isn't a valid choice: use no whitespace, '(', ')' or '='")):
        choice(text)


@pytest.mark.parametrize("name", ["minimal", "core-only", "accounts_taskiq", "accounts-api_keys2"])
def test_a_preset_or_verify_set_name_may_use_hyphens_and_underscores(name: str) -> None:
    assert label(name, "preset") == name


@pytest.mark.parametrize("name", ["Minimal", "-core", "_core", "2fa", "core only", ""])
def test_any_other_preset_or_verify_set_name_is_refused(name: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{name!r} isn't a valid verify set name: use lowercase letters")):
        label(name, "verify set")


@pytest.mark.parametrize("name", ["fastro", "fastroai", "my-blueprint2"])
def test_a_blueprint_name_may_also_use_hyphens(name: str) -> None:
    assert blueprint_name(name) == name


@pytest.mark.parametrize("name", ["Fastro", "my_blueprint", "-fastro", "2fast", ""])
def test_any_other_blueprint_name_is_refused(name: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{name!r} isn't a valid blueprint name")):
        blueprint_name(name)


@pytest.mark.parametrize(
    ("text", "subtree"),
    [
        ("backend/src/modules/tier/", True),
        ("backend/scripts/create_first_tier.py", False),
        ("README.md", False),
        ("frontend/", True),
    ],
)
def test_a_path_is_one_file_or_with_a_trailing_slash_a_subtree(text: str, subtree: bool) -> None:
    assert repo_path(text) == RepoPath(text)
    assert repo_path(text).subtree is subtree


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("/backend/src/", "absolute"),
        ("backend/../secrets/", "'..'"),
        ("../elsewhere.py", "'..'"),
        ("./backend/", "'.'"),
        ("backend//src/", "an empty segment"),
        ("backend\\src\\", "a backslash"),
        ("backend/src/*.py", "a glob"),
        ("backend/src/mod?.py", "a glob"),
        ("backend/[ab].py", "a glob"),
        ("", "empty"),
        ("/", "absolute"),
    ],
)
def test_a_path_that_isnt_posix_relative_and_literal_is_refused(text: str, why: str) -> None:
    with pytest.raises(SpecError, match=f"{re.escape(repr(text))} isn't a repo path: .*{re.escape(why)}"):
        repo_path(text)


@pytest.mark.parametrize(
    ("text", "ref"),
    [
        ("src.modules.tier.routes:router", Ref("src.modules.tier.routes", "router")),
        ("src.modules.tier.routes:router as tiers_router", Ref("src.modules.tier.routes", "router", "tiers_router")),
        ("scripts.create_first_tier:create_first_tier", Ref("scripts.create_first_tier", "create_first_tier")),
        ("m:X", Ref("m", "X")),
    ],
)
def test_a_ref_is_a_module_an_attribute_and_maybe_an_alias(text: str, ref: Ref) -> None:
    assert Ref.parse(text) == ref
    assert str(ref) == text


def test_a_refs_local_name_is_its_alias_or_else_its_attribute() -> None:
    assert Ref.parse("src.modules.tier.routes:router as tiers_router").name == "tiers_router"
    assert Ref.parse("src.modules.tier.routes:router").name == "router"


@pytest.mark.parametrize(
    "text",
    [
        "src.modules.tier.routes",
        ":router",
        "src.modules.tier.routes:",
        ".modules.tier:router",
        "src..tier:router",
        "src.modules.tier:2router",
        "src.modules.tier:router as",
        "src.modules.tier:router as 2r",
        "src.modules.tier:router as r extra",
        "src.class.tier:router",
        "src.modules.tier:class",
        "src.modules.tier:router as import",
        "src.modules.tier:a.b",
    ],
)
def test_any_other_ref_is_refused(text: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{text!r} isn't a ref: expected 'package.module:name'")):
        Ref.parse(text)


@pytest.mark.parametrize("text", ["0.21.0", "1.2.3", "0.1.0.dev0", "0.21.0rc1", "2026.10"])
def test_a_version_is_pep_440(text: str) -> None:
    assert version(text) == Version(text)


@pytest.mark.parametrize("text", ["latest", "0.21.0-final-final", "0.21.0+", ""])
def test_any_other_version_is_refused(text: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{text!r} isn't a PEP 440 version")):
        version(text)


@pytest.mark.parametrize("text", ["0.21", "=>0.20", ">=0.20,<", "~=1"])
def test_a_specifier_that_isnt_pep_440_is_refused(text: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{text!r} isn't a PEP 440 version specifier")):
        specifier(text)


@pytest.mark.parametrize(
    ("spec", "candidate", "allowed"),
    [
        (">=0.20,<0.22", "0.21.0", True),
        (">=0.20,<0.22", "0.22.0", False),
        (">=0.20,<0.22", "0.21.0rc1", False),
        (">=0.20,<0.22", "0.21.0.dev3", False),
        (">=0.21.0rc1,<0.22", "0.21.0rc2", True),
        ("==0.21.0rc1", "0.21.0rc1", True),
    ],
)
def test_a_specifier_allows_a_prerelease_only_when_it_names_one(spec: str, candidate: str, allowed: bool) -> None:
    assert allows(specifier(spec), version(candidate)) is allowed


@pytest.mark.parametrize(
    ("spec", "tool", "allowed"),
    [
        (">=0.1.0.dev0,<0.2", "0.1.0.dev0", True),
        (">=0.1.0.dev0,<0.2", "0.1.0", True),
        (">=0.1.0.dev0,<0.2", "0.1.5rc1", True),
        (">=0.1.0.dev0,<0.2", "0.2.0", False),
        (">=0.1,<0.2", "0.1.0.dev0", False),
        (">=0.1,<0.2", "0.1.1.dev0", True),
    ],
)
def test_requires_fastro_lets_development_builds_in_above_its_lower_bound(spec: str, tool: str, allowed: bool) -> None:
    assert tool_allowed(specifier(spec), version(tool)) is allowed


@pytest.mark.parametrize("text", ["fastapi[standard]>=0.115.8", "crudauth[all]>=0.8.2,<0.9.0", "greenlet", "stripe>=11.0"])
def test_a_dependency_is_a_pep_508_requirement(text: str) -> None:
    assert requirement(text) == Requirement(text)


@pytest.mark.parametrize("text", ["fastapi >= ", "fast api", ">=1.0", ""])
def test_any_other_dependency_is_refused(text: str) -> None:
    with pytest.raises(SpecError, match=re.escape(f"{text!r} isn't a PEP 508 requirement")):
        requirement(text)
