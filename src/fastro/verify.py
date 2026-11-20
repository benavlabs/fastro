"""``fastro blueprint verify``: build each verify set through the engine into a scratch directory, and check it.

A port of track B's removal drill: its report and its "leave the scratch copy on failure" behaviour are kept, but a
set is built by plan, render and apply from the repo's tracked files, never by copying the repo and deleting.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from fastro import lint as linting
from fastro import pyproject
from fastro.apply import ApplyError, Change, apply, breaks_the_rule, changes, file_kind, tracked_files
from fastro.engine import catalog_check, envfile
from fastro.engine.blueprint import Blueprint
from fastro.engine.plan import plan
from fastro.engine.problems import CatalogError
from fastro.engine.render import RenderError, render
from fastro.engine.resolve import Selection, SelectionError, select
from fastro.engine.spec import RepoPath
from fastro.runner import available, declared_command, run_captured
from fastro.tools import require

EVERYTHING = "everything"
LOCK = "uv.lock"
REPORTED_LINES = 40
REFUSED = "verify can't build a set:"
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


def starting_lock(catalog: Blueprint, tracked: Sequence[RepoPath]) -> RepoPath | None:
    """The lock a build starts from (spec 2.13): ``{python_root}/uv.lock`` when it's tracked, otherwise a tracked
    ``uv.lock`` at the repo root, otherwise none."""
    names = {path.text for path in tracked}
    for candidate in (_python_root_lock(catalog), LOCK):
        if candidate in names:
            return RepoPath(candidate)
    return None


def build(
    root: Path, catalog: Blueprint, features: Sequence[str], into: Path, tracked: Sequence[RepoPath] | None = None
) -> Path:
    """The project ``features`` give, built into ``into`` from the tracked files of the blueprint repo at ``root``
    (``tracked``, or git's list when it's ``None``): what the plan copies, the wiring it renders, its
    ``{python_root}/pyproject.toml`` and ``{python_root}/.env.example`` rewritten for the selection (6.3, 6.1), and
    the lock it starts from as ``{python_root}/uv.lock``.

    Raises:
        Unbuildable: the wiring can't be rendered, the selection's runtime dependencies can't be merged, or
            ``.env.example`` has a problem 6.1 refuses.
        ApplyError: a file the build would copy is one apply refuses.
    """
    files = tracked_files(root) if tracked is None else tracked
    selection = select(catalog, features)
    made = plan(catalog, selection, files)
    try:
        rendered = render(made)
    except (RenderError, SelectionError) as error:
        raise Unbuildable(str(error)) from None
    found = changes(made, rendered)
    python_root = catalog.info.python_root.text.rstrip("/")
    rewrites: dict[str, Callable[[str, Blueprint, Selection], str]] = {
        f"{python_root}/pyproject.toml": pyproject.rewrite,
        f"{python_root}/.env.example": envfile.process,
    }
    for index, change in enumerate(found):
        rewrite = rewrites.get(change.path)
        if rewrite is None or breaks_the_rule(root, change.path) is not None:
            continue
        try:
            text = rewrite((root / change.path).read_bytes().decode("utf-8"), catalog, selection)
        except (pyproject.PyprojectError, envfile.EnvFileError) as error:
            raise Unbuildable(str(error)) from None
        found[index] = Change("write", change.path, text)
    lock = starting_lock(catalog, files)
    if lock is not None:
        found.append(Change("copy", _python_root_lock(catalog), source=lock.text))
    apply(found, root, into)
    return into


def _readable(root: Path, catalog: Blueprint, tracked: Sequence[RepoPath]) -> None:
    """Refuse the run when ``{python_root}/pyproject.toml`` gives a build no name, or a tracked
    ``{python_root}/.env.example`` isn't UTF-8 (spec 2.13).

    Raises:
        Unbuildable: ``pyproject.toml`` isn't tracked, 1.10 rules it out, it isn't UTF-8, it doesn't parse, or it has
            no ``[project].name`` string that's a valid name by section 3's rule; or ``.env.example`` is tracked, 1.10
            doesn't rule it out, and it isn't UTF-8 or has a problem 6.1 refuses.
        OSError: the system won't let fastro resolve or open either file.
    """
    python_root = catalog.info.python_root.text.rstrip("/")
    names = {path.text for path in tracked}
    file = f"{python_root}/pyproject.toml"
    table = linting.project_table(root, file, names, "verify", strict=True)
    if isinstance(table, linting.Unread):
        raise Unbuildable(f"{REFUSED} {file} {table.reason}. {table.fix}")
    if (problem := linting.name_problem(file, table)) is not None:
        raise Unbuildable(f"{REFUSED} {problem} {linting.name_fix(file)}")
    example = f"{python_root}/.env.example"
    if example not in names or breaks_the_rule(root, example) is not None:
        return
    text = linting.env_text(root, example)
    if text is None:
        raise Unbuildable(f"{REFUSED} {example} isn't UTF-8 text. Save {example} as UTF-8.")
    if found := envfile.problems(text, catalog):
        raise Unbuildable(f"{REFUSED} {example} {found[0]} Run fastro blueprint lint to see every problem.")


def _python_root_lock(catalog: Blueprint) -> str:
    return f"{catalog.info.python_root.text.rstrip('/')}/{LOCK}"


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
        Unbuildable: ``{python_root}/pyproject.toml`` gives a build no name, or a tracked
            ``{python_root}/.env.example`` isn't UTF-8 or has a problem 6.1 refuses, and nothing has been built; or a
            set's wiring can't be rendered or its runtime dependencies can't be merged (6.3), and the run ends there.
        ApplyError: a set's build is refused. The run ends there.
        OSError: the system won't let a build read or write a file. The run ends there.
    """
    unknown(catalog, names)
    sets = verify_sets(catalog)
    tracked = tracked_files(root)
    _readable(root, catalog, tracked)
    lock = starting_lock(catalog, tracked)
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
            print(f"  lock: {lock.text if lock else 'none'}")
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

    The built-in ones run from ``python_root``. First ``install``: ``uv sync --project {python_root}`` with the dev
    dependencies from wherever the build's ``pyproject.toml`` keeps them (spec 2.13, 6.3). Then, through the build's
    interpreter, ``uv run --project {python_root}`` with the same dev dependencies: the app import (when
    ``[verify].app`` is set), every module importing, ruff, mypy and the tests. The migration baseline, when there's
    a ``db baseline`` command, runs that declared command as spec 2.9 runs it. Then each ``[[verify.checks]]`` whose
    ``if`` holds runs from its ``cwd``. Each build of a run, ``scratch/<set>``, has its own environment at
    ``scratch/.venvs/<set>``. The environment is fastro's, without ``VIRTUAL_ENV``, ``PYTHONPATH`` and ``PYTHONHOME``, which
    would point the checks outside the build, and with ``[checks.toolchain.env]`` on top.
    """
    root = catalog.info.import_root
    environment = {name: value for name, value in os.environ.items() if name not in OUTSIDE_THE_BUILD}
    environment.update(catalog.toolchain_env)

    def run(project: Path, selection: Selection) -> list[Result]:
        shared = {**environment, "UV_PROJECT_ENVIRONMENT": str(project.parent / ".venvs" / project.name)}
        backend = (project / catalog.info.python_root.text.rstrip("/")).resolve()
        dev = _dev_dependencies(backend / "pyproject.toml")
        python = [require("uv"), "run", "--project", str(backend), *dev, "python"]
        present, blocked = _directories(backend, (root, "scripts", "migrations", "tests"))
        steps: list[tuple[str, list[str], Path]] = [
            ("install", [require("uv"), "sync", "--project", str(backend), *dev], backend)
        ]
        if catalog.verify.app is not None:
            steps.append(("app imports", [*python, "-c", QUIET + f"import {catalog.verify.app}"], backend))
        steps.append(("every module imports", [*python, "-c", QUIET + EVERY_MODULE.format(root=root)], backend))
        linted = [name for name in (root, "tests") if name in present]
        steps.append(("ruff", [*python, "-m", "ruff", "check", *linted], backend))
        steps.append(("mypy", [*python, "-m", "mypy", *present, "--config-file", "pyproject.toml"], backend))
        if "tests" in present or "tests" in blocked:
            steps.append(("tests", [*python, "-m", "pytest", "tests", "-q", "-p", "no:randomly"], backend))
        baseline = available(catalog, selection).get(BASELINE)
        if baseline is not None:
            argv, cwd = declared_command(baseline, project, catalog.info.python_root, ["--check"])
            steps.append(("migrations", argv, cwd))
        for check in catalog.verify.checks:
            if check.condition is None or selection.holds(check.condition):
                cwd = project / check.cwd.text.rstrip("/") if check.cwd is not None else project
                steps.append((check.name, list(check.run), cwd))
        needs = {"ruff": (root, "tests"), "mypy": (root, "scripts", "migrations", "tests"), "tests": ("tests",)}
        results = []
        for name, argv, cwd in steps:
            reason = next((blocked[needed] for needed in needs.get(name, ()) if needed in blocked), None)
            results.append(Result(name, False, reason, reason) if reason else _step(name, argv, cwd, shared))
        return results

    return run


def _directories(backend: Path, names: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """Which of ``names`` are directories under ``backend``, and, for each one fastro isn't allowed to look at, the
    line saying it can't be entered."""
    present, blocked = [], {}
    for name in names:
        try:
            if file_kind(backend / name) == "directory":
                present.append(name)
        except OSError as error:
            blocked[name] = f"{backend / name} can't be entered: {error.strerror}."
    return present, blocked


def _dev_dependencies(pyproject: Path) -> list[str]:
    """The ``uv`` flags that add a build's dev dependencies: ``--extra dev`` for ``[project.optional-dependencies].dev``,
    ``--group dev`` for ``[dependency-groups].dev``, both when both are there, and none when neither is. A run ends
    before it builds anything when ``pyproject.toml`` can't be read, so the empty result for one is only a guard."""
    try:
        data = tomllib.loads(catalog_check.read_text(pyproject))
    except (OSError, CatalogError, tomllib.TOMLDecodeError):
        return []
    project = data.get("project")
    extras = project.get("optional-dependencies") if isinstance(project, dict) else None
    groups = data.get("dependency-groups")
    flags = []
    if isinstance(extras, dict) and "dev" in extras:
        flags += ["--extra", "dev"]
    if isinstance(groups, dict) and "dev" in groups:
        flags += ["--group", "dev"]
    return flags


def _step(name: str, argv: list[str], cwd: Path, environment: dict[str, str]) -> Result:
    """One check, run and read: it passes when it exits 0, and its last printed line is its summary."""
    refused = f"Its cwd, {cwd}, can't be entered."
    try:
        kind = file_kind(cwd)
    except OSError:
        return Result(name, False, refused, refused)
    if kind != "directory":
        missing = f"Its cwd, {cwd}, isn't a directory in this build."
        return Result(name, False, missing, missing)
    if not os.access(cwd, os.X_OK):
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
