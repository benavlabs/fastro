"""``fastro blueprint lint``: a blueprint repo's problems as findings, in spec 7's document (spec 2.13)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastro.apply import RepositoryError, tracked_files
from fastro.engine import blueprint, catalog_check, project
from fastro.engine.blueprint import Blueprint, Command, IncompatibleCatalog
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Problem
from fastro.engine.project import Project
from fastro.engine.spec import RepoPath
from fastro.tools import installed_version

LOAD_PROBLEM = "BP001"
RULES = ("BP001", "BP002", "BP003", "BP005")
CHECKED_ROOTS = ("scripts", "tests")
PATH_FIX = "Fix the path, or add the file to git."
MODULE_FIX = "Point module at a module the repo has, or add its file to git."
SOURCE_FIX = "Add source = true to fastro.toml."


class UsageError(Exception):
    """``blueprint lint`` was run where it can't apply (spec 8: exit 2)."""


class Unreadable(Exception):
    """A file lint needs can't be read (spec 8: exit 3)."""

    def __init__(self, file: Path, error: OSError) -> None:
        super().__init__(f"can't read {file}: {error.strerror}. Check its permissions, then run fastro blueprint lint again.")


@dataclass(frozen=True, order=True)
class Finding:
    """One thing lint found: where, which code, what's wrong and how to fix it. Every finding is an error."""

    file: str
    line: int
    code: str
    message: str
    fix: str = ""
    severity: str = field(default="error", compare=False)


@dataclass(frozen=True)
class Report:
    """What lint found in a repo, with the catalog and project it read, when each loaded."""

    findings: tuple[Finding, ...]
    catalog: Blueprint | None = None
    project: Project | None = None

    def document(self, version: str) -> dict[str, Any]:
        """Spec 7's lint document."""
        info = self.catalog.info if self.catalog is not None else None
        return {
            "tool": "fastro",
            "version": version,
            "schema": 1,
            "project": self.project.name if self.project is not None else None,
            "blueprint": {"name": info.name, "version": str(info.version)} if info is not None else None,
            "tiers": ["static"],
            "rules": list(RULES),
            "counts": {"error": len(self.findings), "warning": 0},
            "findings": [
                {
                    "file": finding.file,
                    "line": finding.line,
                    "code": finding.code,
                    "severity": finding.severity,
                    "message": finding.message,
                    "fix": finding.fix,
                }
                for finding in self.findings
            ],
        }


def lint(root: Path) -> Report:
    """Lint the blueprint repo at ``root``: ``blueprint.toml``, and the ``fastro.toml`` beside it when there is one.

    ``fastro.toml`` must set ``source = true``. When it does, it's checked against the catalog, if the catalog loads;
    when it doesn't, that's the one finding about it, since the repo's catalog isn't the one it pins (2.13).

    The catalog's paths and command modules are checked against the repo's tracked files when it loads (BP002, BP005).

    Raises:
        UsageError: ``root`` has no ``blueprint.toml``, or isn't a git repository.
        MissingToolError: ``git`` isn't on ``PATH``.
        Unreadable: either file can't be read.
        IncompatibleCatalog: the catalog is for another version of fastro.
        NotInstalled: fastro isn't installed.
    """
    file = root / "blueprint.toml"
    if not file.is_file():
        raise UsageError(f"{root} has no blueprint.toml. Run fastro blueprint lint in a blueprint repo.")
    found: list[Problem] = []
    catalog = loaded = None
    tool = installed_version()
    try:
        catalog = blueprint.load("blueprint.toml", _read(file, root), tool)
    except IncompatibleCatalog:
        raise
    except CatalogError as error:
        found.extend(error.problems)
    if catalog is not None:
        try:
            tracked = {path.text for path in tracked_files(root)}
        except RepositoryError as error:
            raise UsageError(f"{error}. Run fastro blueprint lint in a blueprint repo, which git tracks.") from None
        found.extend(_missing_paths(catalog, tracked))
        found.extend(_missing_modules(catalog, tracked))
    toml = root / "fastro.toml"
    if toml.is_file():
        try:
            text = _read(toml, root)
            loaded = project.load("fastro.toml", text)
            if not loaded.source:
                message = "isn't true, and a blueprint repo's fastro.toml sets source = true."
                found.append(loaded.document.problem(("source",), message, SOURCE_FIX))
            elif catalog is not None:
                loaded = project.load("fastro.toml", text, catalog)
        except CatalogError as error:
            found.extend(error.problems)
    return Report(tuple(sorted(_finding(problem) for problem in found)), catalog, loaded)


def human(report: Report, root: Path) -> list[str]:
    """The report as lines: each finding, its code before its key, then a count, or one line when there's nothing."""
    if not report.findings:
        return [f"ok: blueprint lint found nothing to fix in {root}."]
    lines = []
    for finding in report.findings:
        lines.append(f"{finding.file}:{finding.line}: {finding.code} {finding.message}")
        if finding.fix:
            lines.append(f"  {finding.fix}")
    count = len(report.findings)
    lines.append(f"blueprint lint found {count} problem{'' if count == 1 else 's'}.")
    return lines


def _finding(problem: Problem) -> Finding:
    message = f"{problem.key}: {problem.message}" if problem.key else problem.message
    return Finding(problem.file, problem.line, problem.code or LOAD_PROBLEM, message, problem.fix)


def _read(file: Path, root: Path) -> str:
    """``file``'s text, with a problem reading it named by its path from ``root``."""
    try:
        return catalog_check.read_text(file)
    except OSError as error:
        raise Unreadable(file, error) from None
    except CatalogError as error:
        name = file.relative_to(root).as_posix()
        raise CatalogError(dataclasses.replace(problem, file=name) for problem in error.problems) from None


def _missing_paths(catalog: Blueprint, tracked: set[str]) -> list[Problem]:
    """BP002: each feature, ``when``, generator-template and overlay path that isn't among ``tracked`` (2.13)."""
    declared: list[tuple[KeyPath, RepoPath]] = []
    for feature in catalog.features:
        declared += [((*feature.at, "paths", index), path) for index, path in enumerate(feature.paths)]
        for number, when in enumerate(feature.when):
            declared += [((*feature.at, "when", number, "paths", index), path) for index, path in enumerate(when.paths)]
    for generator in catalog.generators.values():
        declared.append(((*generator.at, "templates"), _directory(generator.templates)))
    if catalog.info.overlay is not None:
        declared.append((("blueprint", "overlay"), _directory(catalog.info.overlay)))
    found = []
    for at, path in declared:
        if path.subtree and not any(name.startswith(path.text) for name in tracked):
            found.append(catalog.document.problem(at, f"{path.text!r} holds no tracked file.", PATH_FIX, "BP002"))
        elif not path.subtree and path.text not in tracked:
            found.append(catalog.document.problem(at, f"{path.text!r} isn't a tracked file in the repo.", PATH_FIX, "BP002"))
    return found


def _missing_modules(catalog: Blueprint, tracked: set[str]) -> list[Problem]:
    """BP005: each command whose module, under ``import_root``, ``scripts`` or ``tests``, has no tracked file."""
    commands: list[Command] = [*catalog.commands.values()]
    for feature in catalog.features:
        commands += feature.commands.values()
    found = []
    for command in commands:
        parts = command.module.split(".")
        if parts[0] not in (catalog.info.import_root, *CHECKED_ROOTS):
            continue
        cwd = (command.cwd or catalog.info.python_root).text.rstrip("/")
        module, package = f"{cwd}/{'/'.join(parts)}.py", f"{cwd}/{'/'.join(parts)}/__init__.py"
        if module not in tracked and package not in tracked:
            message = f"{command.module!r} has no file: neither {module} nor {package} is tracked."
            found.append(catalog.document.problem((*command.at, "module"), message, MODULE_FIX, "BP005"))
    return found


def _directory(path: RepoPath) -> RepoPath:
    """``path`` as a directory, with one trailing ``/``, whichever way the catalog wrote it."""
    return RepoPath(path.text.rstrip("/") + "/")
