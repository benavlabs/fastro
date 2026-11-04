"""``fastro blueprint verify``: build each verify set through the engine into a scratch directory, and check it.

A port of track B's removal drill: its report and its "leave the scratch copy on failure" behaviour are kept, but a
set is built by plan, render and apply from the repo's tracked files, never by copying the repo and deleting.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from fastro.apply import apply, changes, tracked_files
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import plan
from fastro.engine.render import render
from fastro.engine.resolve import Selection, select

EVERYTHING = "everything"
REPORTED_LINES = 40


class UnknownSet(Exception):
    """A set asked for isn't one the catalog verifies (spec 8: exit 2)."""


@dataclass(frozen=True)
class Result:
    """What one check printed, and whether it passed."""

    name: str
    ok: bool
    last_line: str
    output: str


Checks = Callable[[Path, Selection], list[Result]]


def verify_sets(catalog: Blueprint) -> dict[str, tuple[str, ...]]:
    """The sets ``blueprint verify`` builds, by name: ``[verify.sets]``, the presets when ``include_presets`` holds,
    and ``everything`` (2.2)."""
    sets = dict(catalog.verify.sets)
    if catalog.verify.include_presets:
        sets.update((name, features) for name, features in catalog.presets.items() if name not in sets)
    sets[EVERYTHING] = tuple(feature.name for feature in catalog.features)
    return sets


def build(root: Path, catalog: Blueprint, features: Sequence[str], into: Path) -> Path:
    """The project ``features`` give, built into ``into`` from the tracked files of the blueprint repo at ``root``:
    what the plan copies, and the wiring it renders."""
    made = plan(catalog, select(catalog, features), tracked_files(root))
    apply(changes(made, render(made)), root, into)
    return into


def report(result: Result) -> str:
    """One check's line, followed by the end of what it printed when it failed."""
    line = f"  {'PASS' if result.ok else 'FAIL'}  {result.name:12} {result.last_line}"
    if result.ok:
        return line
    printed = result.output.splitlines()
    dropped = len(printed) - REPORTED_LINES
    kept = [f"... {dropped} earlier lines"] if dropped > 0 else []
    kept += printed[-REPORTED_LINES:]
    return "\n".join([line, *(f"        {printed_line}" for printed_line in kept)])


def verify(root: Path, catalog: Blueprint, names: Sequence[str], checks: Checks, keep: bool) -> int:
    """Build each set ``names`` asks for, every set when it's empty, run ``checks`` on it, and print the report.

    The scratch directory is removed when every check passes, unless ``keep``; otherwise its path is printed.

    Raises:
        UnknownSet: a name isn't a verify set. Nothing has been built.
    """
    sets = verify_sets(catalog)
    for name in names:
        if name not in sets:
            raise UnknownSet(f"{name!r} isn't a verify set. The sets are {', '.join(sets)}.")
    scratch = Path(tempfile.mkdtemp(prefix="fastro-verify-"))
    failures = 0
    try:
        for name in names or list(sets):
            project = build(root, catalog, sets[name], scratch / name)
            selection = select(catalog, sets[name])
            print(f"\n=== {name}: {', '.join(sorted(selection.features)) or 'core only'}")
            for result in checks(project, selection):
                print(report(result))
                failures += not result.ok
    except Exception:
        failures += 1
        raise
    finally:
        if keep or failures:
            print(f"\nscratch copies left in {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    return 1 if failures else 0
