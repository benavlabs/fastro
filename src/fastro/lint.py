"""``fastro blueprint lint``: a blueprint repo's problems as findings, in spec 7's document (spec 2.13)."""

from __future__ import annotations

import ast
import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastro import make
from fastro.apply import RepositoryError, present, tracked_files, unreadable, unreadable_config
from fastro.engine import blueprint, catalog_check, project
from fastro.engine.blueprint import MODULE_GENERATOR, Blueprint, Command, IncompatibleCatalog
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Problem
from fastro.engine.project import Project
from fastro.engine.spec import Ref, RepoPath
from fastro.tools import installed_version

LOAD_PROBLEM = "BP001"
RULES = ("BP001", "BP002", "BP003", "BP004", "BP005", "BP006", "FR014", "FR016")
CHECKED_ROOTS = ("scripts", "tests")
PATH_FIX = "Fix the path, or add the file to git."
MODULE_FIX = "Point module at a module the repo has, or add its file to git."
APP_FIX = "Point app at a module the repo has, or add its file to git."
READS_ONLY = "and lint reads only regular files inside the repo"
READ_FIX = "Make it a regular file in the repo, or point at one that is."
REF_FIX = "Define it there, or point the ref at the module that does."
REF_MODULE_FIX = "Point the ref at a module the repo has, or add its file to git."
PARSE_FIX = "Fix the module so it parses, then run fastro blueprint lint again."
ANY_NAME = None
MARKER = re.compile(r"# @fastro option=(\S+)")
ASSIGNED = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
SOURCE_FIX = "Add source = true to fastro.toml."
COMMAND_CLASH_FIX = "Rename one of the commands, so that no command's words begin another's."
FLAG_CLASH_FIX = "Rename the param, so that its flags are its own."


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
        UsageError: ``root`` has no ``blueprint.toml``, isn't a git repository, or has a ``blueprint.toml`` or
            ``fastro.toml`` fastro may not read (spec 1.10).
        MissingToolError: ``git`` isn't on ``PATH``.
        Unreadable: ``blueprint.toml``, ``fastro.toml``, a module file or ``.env.example`` can't be opened.
        IncompatibleCatalog: the catalog is for another version of fastro.
        NotInstalled: fastro isn't installed.
    """
    file = root / "blueprint.toml"
    try:
        there = present(file)
    except OSError as error:
        raise Unreadable(file, error) from None
    if not there:
        raise UsageError(f"{root} has no blueprint.toml. Run fastro blueprint lint in a blueprint repo.")
    for name in ("blueprint.toml", "fastro.toml"):
        try:
            refused = unreadable_config(root, name)
        except OSError as error:
            raise Unreadable(root / name, error) from None
        if refused is not None:
            raise UsageError(refused)
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
        found.extend(_unresolved_refs(root, catalog, tracked))
        found.extend(_open_verify_sets(catalog))
        found.extend(_name_clashes(catalog))
        found.extend(_unmarked_env(root, catalog, tracked))
    toml = root / "fastro.toml"
    if os.path.lexists(toml):
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
    """BP002: each feature, ``when``, generator template and mode, and overlay path that isn't among ``tracked`` (2.13)."""
    declared: list[tuple[KeyPath, RepoPath]] = []
    for feature in catalog.features:
        declared += [((*feature.at, "paths", index), path) for index, path in enumerate(feature.paths)]
        for number, when in enumerate(feature.when):
            declared += [((*feature.at, "when", number, "paths", index), path) for index, path in enumerate(when.paths)]
    for generator in catalog.generators.values():
        declared.append(((*generator.at, "templates"), _directory(generator.templates)))
        declared += [
            ((*generator.at, "modes", index), RepoPath(f"{_directory(generator.templates).text}{mode}/"))
            for index, mode in enumerate(generator.modes)
        ]
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
    """BP005: each command whose module, under ``import_root``, ``scripts`` or ``tests``, has no tracked file, and
    ``[verify].app`` when its module has none (2.13)."""
    commands: list[Command] = [*catalog.commands.values()]
    for feature in catalog.features:
        commands += feature.commands.values()
    found = []
    declared = [((*command.at, "module"), command.module, command.cwd, MODULE_FIX) for command in commands]
    if catalog.verify.app is not None:
        declared.append((("verify", "app"), catalog.verify.app, None, APP_FIX))
    for at, name, cwd_path, fix in declared:
        parts = name.split(".")
        if parts[0] not in (catalog.info.import_root, *CHECKED_ROOTS):
            continue
        cwd = (cwd_path or catalog.info.python_root).text.rstrip("/")
        module, package = f"{cwd}/{'/'.join(parts)}.py", f"{cwd}/{'/'.join(parts)}/__init__.py"
        if module not in tracked and package not in tracked:
            message = f"{name!r} has no file: neither {module} nor {package} is tracked."
            found.append(catalog.document.problem(at, message, fix, "BP005"))
    return found


def _directory(path: RepoPath) -> RepoPath:
    """``path`` as a directory, with one trailing ``/``, whichever way the catalog wrote it."""
    return RepoPath(path.text.rstrip("/") + "/")


def _declared_refs(catalog: Blueprint) -> list[tuple[KeyPath, Ref]]:
    """Every ref the catalog declares (1.4): contributions to a slot that takes refs, and a ``class`` slot's
    ``fallback`` and ``append`` and a ``reexport`` slot's ``fallback``, each with where it's written."""
    values = {slot.point for file in catalog.wiring for slot in file.slots if slot.literal or slot.kind == "data"}
    groups = [catalog.core]
    for feature in catalog.features:
        groups += [feature.contributes, *(when.contributes for when in feature.when)]
    found: list[tuple[KeyPath, Ref]] = []
    for group in groups:
        for point, items in group.items():
            if point in values:
                continue
            for item in items:
                target = item.target
                found.append((item.at, target if isinstance(target, Ref) else Ref.parse(str(target))))
    for file in catalog.wiring:
        for slot in file.slots:
            if isinstance(slot.fallback, Ref):
                found.append(((*slot.at, "fallback"), slot.fallback))
            found += [((*slot.at, "append", index), ref) for index, ref in enumerate(slot.append)]
    return found


def _unresolved_refs(root: Path, catalog: Blueprint, tracked: set[str]) -> list[Problem]:
    """FR014: each ref, to a module under ``import_root``, ``scripts`` or ``tests``, whose module's tracked file
    doesn't bind its name at module level. The file is parsed, never imported (2.13)."""
    python_root = catalog.info.python_root.text.rstrip("/")
    bound: dict[str, frozenset[str] | str | None] = {}
    found = []
    for at, ref in _declared_refs(catalog):
        parts = ref.module.split(".")
        if parts[0] not in (catalog.info.import_root, *CHECKED_ROOTS):
            continue
        module, package = f"{python_root}/{'/'.join(parts)}.py", f"{python_root}/{'/'.join(parts)}/__init__.py"
        reasons = {path: unreadable(root, path) for path in (module, package) if path in tracked}
        file = next((path for path, reason in reasons.items() if reason is None), None)
        if file is None and reasons:
            first, reason = next(iter(reasons.items()))
            message = f"{str(ref)!r} can't be checked: {first} {reason}, {READS_ONLY}."
            found.append(catalog.document.problem(at, message, READ_FIX, "FR014"))
            continue
        if file is None:
            message = f"{str(ref)!r} names a module the repo doesn't have: neither {module} nor {package} is tracked."
            found.append(catalog.document.problem(at, message, REF_MODULE_FIX, "FR014"))
            continue
        if file not in bound:
            bound[file] = _module_names(root / file)
        names = bound[file]
        if isinstance(names, str):
            message = f"{str(ref)!r} can't be checked: {file} doesn't parse ({names})."
            found.append(catalog.document.problem(at, message, PARSE_FIX, "FR014"))
        elif names is not ANY_NAME and ref.attr not in names:
            message = f"{str(ref)!r} names {ref.attr!r}, which {file} doesn't define at module level."
            found.append(catalog.document.problem(at, message, REF_FIX, "FR014"))
    return found


def _module_names(file: Path) -> frozenset[str] | str | None:
    """The names ``file`` binds at module level, ``None`` when a star import could bind any, or why it doesn't
    parse. Top-level ``if``, ``try`` and ``with`` blocks count as module level.

    Raises:
        Unreadable: the system won't let lint open ``file``.
    """
    try:
        source = file.read_bytes()
    except OSError as error:
        raise Unreadable(file, error) from None
    try:
        tree = ast.parse(source, str(file))
    except SyntaxError as error:
        return f"{error.msg} on line {error.lineno}" if error.lineno else error.msg
    names: set[str] = set()
    pending: list[ast.stmt] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(name for target in node.targets for name in _targets(target))
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            names.update(_targets(node.target))
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                return ANY_NAME
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.If | ast.With | ast.AsyncWith):
            pending += [*node.body, *(node.orelse if isinstance(node, ast.If) else [])]
        elif isinstance(node, ast.Try | ast.TryStar):
            pending += [*node.body, *node.orelse, *node.finalbody]
            pending += [statement for handler in node.handlers for statement in handler.body]
    return frozenset(names)


def _targets(target: ast.expr) -> list[str]:
    """The names an assignment target binds: a name, or the names a tuple, list or starred target unpacks into."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _targets(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _targets(element)]
    return []


def _open_verify_sets(catalog: Blueprint) -> list[Problem]:
    """BP004: each verify set entry whose feature requires one the set doesn't list (2.13)."""
    requires = {feature.name: feature.requires for feature in catalog.features}
    found = []
    for name, listed in catalog.verify.sets.items():
        for index, feature in enumerate(listed):
            for required in requires.get(feature, ()):
                if required not in listed:
                    message = f"lists {feature!r}, which requires {required!r}, and the set doesn't list it."
                    at = ("verify", "sets", name, index)
                    found.append(catalog.document.problem(at, message, f"Add {required!r} to the set.", "BP004"))
    return found


def _name_clashes(catalog: Blueprint) -> list[Problem]:
    """BP006: each command whose words begin another's, naming the first longer one in declaration order, and each
    param of a generator but ``module`` whose flag clashes with make's own or an earlier param's (2.13)."""
    commands: list[Command] = [*catalog.commands.values()]
    for feature in catalog.features:
        commands += feature.commands.values()
    commands.sort(key=lambda command: catalog.document.line(command.at))
    found = []
    for command in commands:
        size = len(command.words)
        longer = next((other for other in commands if len(other.words) > size and other.words[:size] == command.words), None)
        if longer is not None:
            shown, other = " ".join(command.words), " ".join(longer.words)
            message = f"{shown!r} begins the command {other!r}, and fastro {shown} can't be both a command and a group."
            found.append(catalog.document.problem(command.at, message, COMMAND_CLASH_FIX, "BP006"))
    for name, generator in catalog.generators.items():
        if name != MODULE_GENERATOR:
            for param, reason in make.clashes(generator):
                found.append(catalog.document.problem(param.at, reason, FLAG_CLASH_FIX, "BP006"))
    return found


def _unmarked_env(root: Path, catalog: Blueprint, tracked: set[str]) -> list[Problem]:
    """FR016: each option ``env`` VAR without exactly one ``# @fastro option=`` marker directly above its line in
    ``{python_root}/.env.example`` (6.1, 2.13)."""
    example = f"{catalog.info.python_root.text.rstrip('/')}/.env.example"
    reason = "isn't a tracked file" if example not in tracked else unreadable(root, example)
    markers = _markers(root / example) if reason is None else None
    found = []
    for feature in catalog.features:
        for option in feature.options.values():
            key = f"{feature.name}.{option.name}"
            marker = f"'# @fastro option={key}'"
            for var in option.env:
                at = (*option.at, "env", var)
                lines = markers.get((key, var), []) if markers is not None else []
                fix = f"Put {marker} on its own line, directly above {var}= in {example}."
                if markers is None and example not in tracked:
                    message = f"has no {marker} marker, since {example} {reason}."
                elif markers is None:
                    message = f"has no {marker} marker, since {example} {reason}, {READS_ONLY}."
                    fix = f"Make {example} a regular file in the repo."
                elif not lines:
                    message = f"has no {marker} marker in {example}."
                elif len(lines) > 1:
                    listed = ", ".join(map(str, lines[:-1])) + f" and {lines[-1]}"
                    message = f"has {len(lines)} {marker} markers in {example}, on lines {listed}, and needs one."
                    fix = "Keep one of them."
                else:
                    continue
                found.append(catalog.document.problem(at, message, fix, "FR016"))
    return found


def _markers(file: Path) -> dict[tuple[str, str], list[int]]:
    """Each ``(feature.option, VAR)`` a marker line names with the line under it, and the markers' line numbers.

    Raises:
        Unreadable: the system won't let lint open ``file``.
    """
    try:
        data = file.read_bytes()
    except OSError as error:
        raise Unreadable(file, error) from None
    lines = data.decode("utf-8", "replace").splitlines()
    found: dict[tuple[str, str], list[int]] = {}
    for number, (line, below) in enumerate(zip(lines, lines[1:], strict=False), start=1):
        marker, assigned = MARKER.fullmatch(line.rstrip()), ASSIGNED.match(below)
        if marker is not None and assigned is not None:
            found.setdefault((marker.group(1), assigned.group(1)), []).append(number)
    return found
