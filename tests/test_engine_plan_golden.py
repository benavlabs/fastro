"""A2's "Done when": golden plans for mini, and the brief's three properties over any selection of it."""

import itertools
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from packaging.version import Version

from fastro.engine import blueprint
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import plan
from fastro.engine.resolve import Ownership, Selection, select
from fastro.engine.spec import RepoPath

MINI = Path(__file__).parent / "fixtures" / "blueprints" / "mini"
PLANS = Path(__file__).parent / "fixtures" / "plans"
CATALOG = blueprint.load("blueprint.toml", (MINI / "blueprint.toml").read_text(), Version("0.1.0"))
FILES = tuple(RepoPath(str(path.relative_to(MINI))) for path in sorted(MINI.rglob("*")) if path.is_file())
SUBTREES = tuple(path.text for feature in CATALOG.features for path in feature.paths if path.subtree)
WIRING = {file.path.text for file in CATALOG.wiring}
OWNERSHIP = Ownership(CATALOG)
NAMES = tuple(feature.name for feature in CATALOG.features)
CHOICES = {(feature.name, name): option.choices for feature in CATALOG.features for name, option in feature.options.items()}
PROPERTIES = settings(database=None, deadline=None, derandomize=True, max_examples=200)


def _selection(catalog: Blueprint, name: str) -> Selection:
    if name == "paddle":
        return select(catalog, ("billing",), options={("billing", "provider"): "paddle"})
    return select(catalog, catalog.presets[name])


@pytest.mark.parametrize("name", ["minimal", "full", "paddle"])
def test_the_plan_for_mini_matches_its_golden_file(name: str) -> None:
    golden = json.loads((PLANS / f"mini-{name}.json").read_text())

    assert plan(CATALOG, _selection(CATALOG, name), FILES).document() == golden


@st.composite
def selections(draw: st.DrawFn) -> Selection:
    features = draw(st.sets(st.sampled_from(NAMES)))
    options = {key: draw(st.sampled_from(choices)) for key, choices in CHOICES.items()}
    return select(CATALOG, sorted(features), options=options)


def _every_selection() -> list[Selection]:
    """Every selection ``selections()`` can draw: each subset of features, with each combination of choices."""
    subsets = [names for size in range(len(NAMES) + 1) for names in itertools.combinations(NAMES, size)]
    combos = [dict(zip(CHOICES, picked, strict=True)) for picked in itertools.product(*CHOICES.values())]
    return [select(CATALOG, names, options=combo) for names in subsets for combo in combos]


@st.composite
def trees(draw: st.DrawFn) -> tuple[RepoPath, ...]:
    extra = draw(st.lists(st.tuples(st.sampled_from(SUBTREES), st.from_regex(r"[a-z]{1,8}\.py", fullmatch=True))))
    return (*FILES, *(RepoPath(f"{subtree}{name}") for subtree, name in extra))


def _ops(selection: Selection, files: tuple[RepoPath, ...]) -> list[tuple[str, str]]:
    result = plan(CATALOG, selection, files)
    return [(op.path, op.op) for op in result.ops if op.path not in WIRING and op.reason != "it's blueprint_only"]


@PROPERTIES
@given(selections(), trees())
def test_every_selected_features_files_are_copied(selection: Selection, files: tuple[RepoPath, ...]) -> None:
    for path, op in _ops(selection, files):
        owner = OWNERSHIP.of(RepoPath(path))
        if owner.feature in selection.features and owner.when is None:
            assert op == "copy", path


@PROPERTIES
@given(selections(), trees())
def test_no_unselected_features_files_are_copied(selection: Selection, files: tuple[RepoPath, ...]) -> None:
    for path, op in _ops(selection, files):
        owner = OWNERSHIP.of(RepoPath(path))
        if owner.feature and owner.feature not in selection.features:
            assert op == "skip", path


@PROPERTIES
@given(selections(), trees())
def test_when_paths_follow_their_condition(selection: Selection, files: tuple[RepoPath, ...]) -> None:
    features = {feature.name: feature for feature in CATALOG.features}
    for path, op in _ops(selection, files):
        owner = OWNERSHIP.of(RepoPath(path))
        if owner.when is not None and owner.feature in selection.features:
            holds = selection.holds(features[owner.feature].when[owner.when].condition)
            assert op == ("copy" if holds else "skip"), path


def test_the_selections_drawn_reach_both_sides_of_every_when() -> None:
    drawable = _every_selection()

    for feature in CATALOG.features:
        for when in feature.when:
            outcomes = {selection.holds(when.condition) for selection in drawable if feature.name in selection.features}
            assert outcomes == {True, False}, f"{feature.name}: {when.condition_text}"
