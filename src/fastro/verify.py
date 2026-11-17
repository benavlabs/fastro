"""``fastro blueprint verify``: build each verify set through the engine into a scratch directory, and check it.

A port of track B's removal drill: its report and its "leave the scratch copy on failure" behaviour are kept, but a
set is built by plan, render and apply from the repo's tracked files, never by copying the repo and deleting.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from fastro.apply import ApplyError, apply, changes, tracked_files
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import plan
from fastro.engine.render import render
from fastro.engine.resolve import Selection, select
from fastro.engine.spec import RepoPath
from fastro.runner import available, declared_command, run_captured
from fastro.tools import require

EVERYTHING = "everything"
REPORTED_LINES = 40
BASELINE = ("db", "baseline")
OUTSIDE_THE_BUILD = ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")
QUIET = "import warnings, logging; warnings.simplefilter('ignore'); logging.disable(50); "
EVERY_MODULE = """
import importlib, pathlib, sys

broken = []
for path in sorted(pathlib.Path({root!r}).rglob("*.py")):
    module = ".".join(path.with_suffix("").parts).removesuffix(".__init__")
    try:
        importlib.import_module(module)
    except Exception as error:
        broken.append(f"{{module}}: {{type(error).__name__}}: {{error}}")

if broken:
    sys.exit("modules left behind by the removed features: " + "; ".join(broken))

print("every module imports")
"""


class UnknownSet(Exception):
    """A set asked for isn't one the catalog verifies (spec 8: exit 2)."""


class Unbuildable(Exception):
    """The catalog loaded, but a build of it can't be rendered: a problem in ``blueprint.toml`` (spec 8: exit 1)."""


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


def build(
    root: Path, catalog: Blueprint, features: Sequence[str], into: Path, tracked: Sequence[RepoPath] | None = None
) -> Path:
    """The project ``features`` give, built into ``into`` from the tracked files of the blueprint repo at ``root``
    (``tracked``, or git's list when it's ``None``): what the plan copies, and the wiring it renders.

    Raises:
        Unbuildable: the wiring can't be rendered.
        ApplyError: a file the build would copy is one apply refuses.
    """
    made = plan(catalog, select(catalog, features), tracked_files(root) if tracked is None else tracked)
    try:
        rendered = render(made)
    except ValueError as error:
        raise Unbuildable(str(error)) from None
    apply(changes(made, rendered), root, into)
    return into


def unknown(catalog: Blueprint, names: Sequence[str]) -> None:
    """Refuse a name in ``names`` that isn't a verify set.

    Raises:
        UnknownSet: one isn't.
    """
    sets = verify_sets(catalog)
    for name in names:
        if name not in sets:
            raise UnknownSet(f"{name!r} isn't a verify set. The sets are {', '.join(sets)}.")


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
        RepositoryError: ``root`` isn't a git repository. Nothing has been built.
        MissingToolError: ``git`` isn't on ``PATH``, and nothing has been built; or a check needs ``uv`` and it isn't.
        Unbuildable: a set's wiring can't be rendered. The run ends there.
        ApplyError: a set's build is refused. The run ends there.
        OSError: the system won't let a build read or write a file. The run ends there.
    """
    unknown(catalog, names)
    sets = verify_sets(catalog)
    tracked = tracked_files(root)
    scratch = Path(tempfile.mkdtemp(prefix="fastro-verify-"))
    failures = 0
    try:
        for name in names or list(sets):
            try:
                project = build(root, catalog, sets[name], scratch / name, tracked)
            except Unbuildable as error:
                raise Unbuildable(f"verify set {name!r} can't be built: {error}") from None
            except ApplyError as error:
                raise ApplyError(f"verify set {name!r} can't be built: {error}") from None
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


def checks_for(catalog: Blueprint) -> Checks:
    """The checks ``blueprint verify`` runs on a build, each a subprocess in it, never in fastro's own process.

    The built-in ones run through the build's interpreter, ``uv run --project {python_root}``, from ``python_root``:
    the app import (when ``[verify].app`` is set), every module importing, ruff, mypy, the tests, and the migration
    baseline through the declared ``db baseline`` command (when there is one). Then each ``[[verify.checks]]`` whose
    ``if`` holds runs from its ``cwd``. Every build of a run, ``scratch/<set>``, shares the environment at
    ``scratch/.venv``. The environment is fastro's, without ``VIRTUAL_ENV``, ``PYTHONPATH`` and ``PYTHONHOME``, which
    would point the checks outside the build, and with ``[checks.toolchain.env]`` on top.
    """
    root = catalog.info.import_root
    environment = {name: value for name, value in os.environ.items() if name not in OUTSIDE_THE_BUILD}
    environment.update(catalog.toolchain_env)

    def run(project: Path, selection: Selection) -> list[Result]:
        shared = {**environment, "UV_PROJECT_ENVIRONMENT": str(project.parent / ".venv")}
        backend = (project / catalog.info.python_root.text.rstrip("/")).resolve()
        python = [require("uv"), "run", "--project", str(backend), "python"]
        present = [name for name in (root, "scripts", "migrations", "tests") if (backend / name).is_dir()]
        steps: list[tuple[str, list[str], Path]] = []
        if catalog.verify.app is not None:
            steps.append(("app imports", [*python, "-c", QUIET + f"import {catalog.verify.app}"], backend))
        steps.append(("every module imports", [*python, "-c", QUIET + EVERY_MODULE.format(root=root)], backend))
        linted = [name for name in (root, "tests") if name in present]
        steps.append(("ruff", [*python, "-m", "ruff", "check", *linted], backend))
        steps.append(("mypy", [*python, "-m", "mypy", *present, "--config-file", "pyproject.toml"], backend))
        if (backend / "tests").is_dir():
            steps.append(("tests", [*python, "-m", "pytest", "tests", "-q", "-p", "no:randomly"], backend))
        baseline = available(catalog, selection).get(BASELINE)
        if baseline is not None:
            argv, cwd = declared_command(baseline, project, catalog.info.python_root, ["--check"])
            steps.append(("migrations", argv, cwd))
        for check in catalog.verify.checks:
            if check.condition is None or selection.holds(check.condition):
                cwd = project / check.cwd.text.rstrip("/") if check.cwd is not None else project
                steps.append((check.name, list(check.run), cwd))
        return [_step(name, argv, cwd, shared) for name, argv, cwd in steps]

    return run


def _step(name: str, argv: list[str], cwd: Path, environment: dict[str, str]) -> Result:
    """One check, run and read: it passes when it exits 0, and its last printed line is its summary."""
    if not cwd.is_dir():
        missing = f"Its cwd, {cwd}, isn't a directory in this build."
        return Result(name, False, missing, missing)
    if not os.access(cwd, os.X_OK):
        refused = f"Its cwd, {cwd}, can't be entered."
        return Result(name, False, refused, refused)
    try:
        code, output = run_captured(argv, cwd, environment)
    except FileNotFoundError:
        missing = f"{argv[0]} isn't on PATH."
        return Result(name, False, missing, missing)
    except OSError as error:
        refused = f"{argv[0]} can't be run: {error.strerror or error}."
        return Result(name, False, refused, refused)
    lines = output.splitlines()
    return Result(name, code == 0, lines[-1] if lines else "", output)
