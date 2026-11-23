"""``fastro blueprint lint``: a blueprint repo's problems as findings, in spec 7's document (spec 2.13)."""

from __future__ import annotations

import ast
import codecs
import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from fastro import make
from fastro import pyproject as rewriting
from fastro import sync as syncing
from fastro.apply import RepositoryError, export_subst, tracked_files
from fastro.engine import bindings, catalog_check, envfile, project
from fastro.engine.blueprint import MODULE_GENERATOR, Blueprint, Command, IncompatibleCatalog
from fastro.engine.files import breaks_the_rule, present, unreadable, unreadable_config
from fastro.engine.plan import plan
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Document, Problem
from fastro.engine.project import PROJECT_NAME, Project
from fastro.engine.render import RenderError, shadows
from fastro.engine.resolve import SelectionError, select
from fastro.engine.spec import Ref, RepoPath

LOAD_PROBLEM = "BP001"
RULES = ("BP001", "BP002", "BP003", "BP004", "BP005", "BP006", "BP007", "FR001", "FR002", "FR014", "FR016")
PATH_FIX = "Fix the path, or add the file to git."
MODULE_FIX = "Point module at a module the repo has, or add its file to git."
APP_FIX = "Point app at a module the repo has, or add its file to git."
READS_ONLY = "and lint reads only regular files inside the repo"
READ_FIX = "Make it a regular file in the repo, or point at one that is."
REF_FIX = "Define it there, or point the ref at the module that does."
REF_MODULE_FIX = "Point the ref at a module the repo has, or add its file to git."
PARSE_FIX = "Fix the module so it parses, then run fastro blueprint lint again."
ANY_NAME = None
SOURCE_FIX = "Add source = true to fastro.toml."
COMMAND_CLASH_FIX = "Rename one of the commands, so that no command's words begin another's."
FLAG_CLASH_FIX = "Rename the param, so that its flags are its own."
OVERLAY_FIX = "Move or rename one of them, so that each path a project gets has one source."
NAME_RULE = "use letters, digits, '.', '_' and '-', starting and ending with a letter or digit, as PEP 508 says."


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
        Unreadable: ``blueprint.toml``, ``fastro.toml``, a module file, ``pyproject.toml`` or ``.env.example`` can't be
            opened.
        IncompatibleCatalog: the catalog is for another version of fastro.
        Extending: the catalog extends another, which this fastro can't merge yet (2.10).
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
    catalog = loaded = checked = None
    tracked: set[str] = set()
    try:
        catalog = syncing.load_catalog(_read(file, root))
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
        found.extend(_unresolved_refs(root, catalog, tracked, catalog.document, _declared_refs(catalog)))
        found.extend(_open_verify_sets(catalog))
        found.extend(_name_clashes(catalog))
        found.extend(_substituted(root, catalog, tracked))
        found.extend(_unmatched_pyproject(root, catalog, tracked))
        found.extend(_unmergeable(catalog))
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
                checked = loaded
        except CatalogError as error:
            found.extend(error.problems)
    if catalog is not None:
        found.extend(_overlay_collisions(catalog, tracked, checked.app.paths if checked is not None else ()))
    if catalog is not None and checked is not None:
        refs = syncing.app_refs(catalog, checked.app.contributes)
        found.extend(_unresolved_refs(root, catalog, tracked, checked.document, refs))
        found.extend(_shadowing(catalog, checked))
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


def _overlay_collisions(catalog: Blueprint, tracked: set[str], app: tuple[RepoPath, ...]) -> list[Problem]:
    """BP003: each tracked overlay file whose project path an ``[app].paths`` entry of the blueprint's own
    ``fastro.toml`` covers, or a tracked file outside ``blueprint_only``, a wiring file or an overlay file before it
    gives a project too, at ``[blueprint].overlay`` (2.1)."""
    if catalog.info.overlay is None:
        return []
    base = catalog.info.overlay.text.rstrip("/") + "/"
    given = {path: repr(path) for path in tracked if not any(only.covers(path) for only in catalog.info.all_blueprint_only)}
    given.update({file.path.text: f"the wiring file {file.path.text!r}" for file in catalog.wiring})
    found = []
    for path in sorted(path for path in tracked if path.startswith(base)):
        target = path.removeprefix(base).removesuffix(".j2")
        entry = next((entry for entry in app if entry.covers(target)), None)
        if entry is not None:
            message = f"{path!r} gives a project {target!r}, which the [app].paths entry {entry.text!r} covers."
            found.append(catalog.document.problem(("blueprint", "overlay"), message, OVERLAY_FIX, "BP003"))
        elif target in given:
            message = f"{path!r} gives a project {target!r}, which {given[target]} gives it too."
            found.append(catalog.document.problem(("blueprint", "overlay"), message, OVERLAY_FIX, "BP003"))
        else:
            given[target] = repr(path)
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
        if parts[0] not in (catalog.info.import_root, *syncing.CHECKED_ROOTS):
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


def _unresolved_refs(
    root: Path, catalog: Blueprint, tracked: set[str], document: Document, refs: list[tuple[KeyPath, Ref]]
) -> list[Problem]:
    """FR014: each of ``refs``, written in ``document``, to a module under ``import_root``, ``scripts`` or ``tests``,
    whose module's tracked file doesn't bind its name at module level. The file is parsed, never imported (2.13)."""
    bound: dict[str, frozenset[str] | str | None] = {}
    found = []
    for at, ref in refs:
        files = syncing.module_files(catalog, ref)
        if files is None:
            continue
        module, package = files
        reasons = {path: unreadable(root, path) for path in (module, package) if path in tracked}
        file = next((path for path, reason in reasons.items() if reason is None), None)
        if file is None and reasons:
            first, reason = next(iter(reasons.items()))
            message = f"{str(ref)!r} can't be checked: {first} {reason}, {READS_ONLY}."
            found.append(document.problem(at, message, READ_FIX, "FR014"))
            continue
        if file is None:
            message = f"{str(ref)!r} names a module the repo doesn't have: neither {module} nor {package} is tracked."
            found.append(document.problem(at, message, REF_MODULE_FIX, "FR014"))
            continue
        if file not in bound:
            bound[file] = _module_names(root / file)
        names = bound[file]
        if isinstance(names, str):
            message = f"{str(ref)!r} can't be checked: {file} doesn't parse ({names})."
            found.append(document.problem(at, message, PARSE_FIX, "FR014"))
        elif names is not ANY_NAME and ref.attr not in names:
            message = f"{str(ref)!r} names {ref.attr!r}, which {file} doesn't define at module level."
            found.append(document.problem(at, message, REF_FIX, "FR014"))
    return found


def _shadowing(catalog: Blueprint, checked: Project) -> list[Problem]:
    """BP001: each ``[app].contributes`` ref, at the ref in ``fastro.toml``, and each catalog contribution, at it in
    ``blueprint.toml``, whose name a wiring file the selection renders binds otherwise (3, 2.13). FR001, at
    ``features``, with ``sync``'s message, when that selection can't be rendered."""
    try:
        selection = select(catalog, checked.features, checked.app.requires, checked.options)
        found = shadows(plan(catalog, selection, ()), checked.app.contributes, checked.document.line)
    except (RenderError, SelectionError) as error:
        return [checked.document.problem(("features",), str(error), "", "FR001")]
    return [checked.document.problem(at, message, syncing.ALIAS_FIX) for at, message in found.app] + [
        catalog.document.problem(at, message, syncing.CATALOG_ALIAS_FIX) for at, message in found.catalog
    ]


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
    found = bindings.bindings(tree)
    return ANY_NAME if any(name == bindings.STAR for name, _ in found) else frozenset(name for name, _ in found)


def _open_verify_sets(catalog: Blueprint) -> list[Problem]:
    """BP004: each verify set entry whose feature requires one the set doesn't list (2.13)."""
    requires = {feature.name: feature.requires for feature in catalog.features}
    found = []
    for name, listed in catalog.verify.sets.items():
        for index, feature in enumerate(listed):
            for required in requires.get(feature, ()):
                if required not in listed:
                    message = f"lists {feature!r}, which requires {required!r}, and the set doesn't list it."
                    at = (*catalog.verify.features_at(name), index)
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


@dataclass(frozen=True)
class Unread:
    """Why a ``pyproject.toml`` can't be read: the end of "since it ...", and how to fix it."""

    reason: str
    fix: str


def _substituted(root: Path, catalog: Blueprint, tracked: set[str]) -> list[Problem]:
    """BP007: each tracked file git's ``export-subst`` attribute is set on, at ``[blueprint]`` (1.7, 2.13)."""
    return [
        catalog.document.problem(
            ("blueprint",),
            f"{path.text} has git's export-subst attribute set, so a tarball of the repo would expand its placeholders "
            "and a clone wouldn't, and every source refuses it.",
            f"Remove export-subst for {path.text} from .gitattributes.",
            "BP007",
        )
        for path in export_subst(root, map(RepoPath, sorted(tracked)))
    ]


def project_table(root: Path, file: str, tracked: set[str], reader: str, strict: bool = False) -> dict[str, Any] | Unread:
    """The ``[project]`` table of ``file``, read as :func:`pyproject_data` reads it, or why ``reader`` can't read it.

    Raises:
        OSError: as :func:`pyproject_data` says.
    """
    data = pyproject_data(root, file, tracked, reader, strict)
    if isinstance(data, Unread):
        return data
    table = data.get("project")
    return table if isinstance(table, dict) else {}


def pyproject_data(root: Path, file: str, tracked: set[str], reader: str, strict: bool = False) -> dict[str, Any] | Unread:
    """``file``, read from its tracked file past a byte order mark, or why ``reader`` can't read it: it isn't tracked,
    1.10 rules it out, it isn't UTF-8 or it doesn't parse (2.13). Lint and verify both read
    ``{python_root}/pyproject.toml`` through it, so they can't disagree.

    Raises:
        OSError: the system won't let fastro open the file, or, when ``strict``, resolve it (1.10).
    """
    if file not in tracked:
        return Unread("isn't a tracked file", f"Add {file} to git.")
    if (reason := (breaks_the_rule if strict else unreadable)(root, file)) is not None:
        return Unread(
            f"{reason}, and {reader} reads only regular files inside the repo", f"Make {file} a regular file in the repo."
        )
    data = (root / file).read_bytes()
    try:
        return tomllib.loads(data.removeprefix(codecs.BOM_UTF8).decode("utf-8"))
    except UnicodeDecodeError:
        return Unread("isn't UTF-8 text", f"Save {file} as UTF-8.")
    except tomllib.TOMLDecodeError as error:
        return Unread(f"isn't valid TOML: {error}", f"Fix {file} so it parses.")


def name_problem(file: str, table: dict[str, Any]) -> str | None:
    """What's wrong with ``[project].name`` in ``file``'s ``table`` by section 3's rule, or ``None`` when it's valid."""
    name = table.get("name")
    if not isinstance(name, str):
        return f"{file} has no [project].name string: {NAME_RULE}"
    if not PROJECT_NAME.fullmatch(name):
        return f"{file} has [project].name {name!r}, which isn't a valid name: {NAME_RULE}"
    return None


def name_fix(file: str) -> str:
    """How to fix a ``[project].name`` :func:`name_problem` reports."""
    return f'Set name under [project] in {file} to a valid PEP 508 name, such as name = "my-api".'


def _unmatched_pyproject(root: Path, catalog: Blueprint, tracked: set[str]) -> list[Problem]:
    """FR002: ``[blueprint].version`` that doesn't equal ``{python_root}/pyproject.toml``'s ``[project].version``
    under PEP 440, or that file without a valid ``[project].name``, each at ``[blueprint].version``; and a declared dev
    dependency no dev table in that file lists, at its entry (2.13).

    Raises:
        Unreadable: the system won't let lint open the file.
    """
    file = f"{catalog.info.python_root.text.rstrip('/')}/pyproject.toml"
    version = catalog.info.version

    def problem(message: str, fix: str) -> Problem:
        return catalog.document.problem(("blueprint", "version"), message, fix, "FR002")

    try:
        data = pyproject_data(root, file, tracked, "lint")
    except OSError as error:
        raise Unreadable(root / file, error) from None
    if isinstance(data, Unread):
        return [problem(f"can't be checked against {file}, since it {data.reason}.", data.fix)]
    table = data.get("project")
    table = table if isinstance(table, dict) else {}
    found = _unlisted_dev(catalog, file, data)
    if (named := name_problem(file, table)) is not None:
        found.append(problem(named, name_fix(file)))
    given = table.get("version")
    version_fix = f'Set version = "{version}" under [project] in {file}, or change [blueprint].version to match it.'
    try:
        parsed = Version(given) if isinstance(given, str) else None
    except InvalidVersion:
        parsed = None
    if parsed is None:
        message = f"can't be compared, since {file} has no [project].version string that's a PEP 440 version."
        found.append(problem(message, version_fix))
    elif parsed != version:
        found.append(problem(f"is {version}, and {file}'s [project].version is {given!r}.", version_fix))
    return found


def _unlisted_dev(catalog: Blueprint, file: str, data: dict[str, Any]) -> list[Problem]:
    """FR002: each ``[blueprint.dependencies].dev`` entry, and each ``dev_dependencies`` entry of a feature or a
    ``when``, whose package no dev table in ``file`` lists, at that entry (2.1, 6.3)."""
    project = data.get("project")
    tables = [project.get("optional-dependencies") if isinstance(project, dict) else None, data.get("dependency-groups")]
    listed = set()
    for table in tables:
        dev = table.get("dev") if isinstance(table, dict) else None
        listed |= {rewriting.package(item) for item in dev} if isinstance(dev, list) else set()
    declared = [(rewriting.CORE, "dev")]
    for feature in catalog.features:
        declared += [(feature.at, "dev_dependencies"), *((when.at, "dev_dependencies") for when in feature.when)]
    found = []
    for path, key in declared:
        for index, text in enumerate(rewriting.written(catalog, path, key)):
            if canonicalize_name(Requirement(text).name) not in listed:
                message = f"{text!r} isn't listed in a dev table of {file}, so no project gets it."
                fix = (
                    f"Add {text!r} to [project.optional-dependencies].dev or [dependency-groups].dev in {file}, or "
                    "remove it here."
                )
                found.append(catalog.document.problem((*path, key, index), message, fix, "FR002"))
    return found


def _unmergeable(catalog: Blueprint) -> list[Problem]:
    """FR002: each runtime dependency entry that 6.3 can't merge with one before it in ``blueprint.toml``, at that
    entry, naming the first. Two entries from different values of one option are never compared (2.13)."""
    declared: list[tuple[KeyPath, KeyPath | None, str | int]] = [(rewriting.CORE, None, "runtime")]
    for feature in catalog.features:
        declared += [(feature.at, None, "dependencies"), *((when.at, None, "dependencies") for when in feature.when)]
        for option in feature.options.values():
            declared += [((*option.at, "dependencies"), option.at, choice) for choice in option.dependencies]
    entries = [
        ((*path, key, index), text, Requirement(text), option, key)
        for path, option, key in declared
        for index, text in enumerate(rewriting.written(catalog, path, key))
    ]
    entries.sort(key=lambda entry: catalog.document.line(entry[0]))
    found = []
    for later, (at, text, requirement, owner, choice) in enumerate(entries):
        name = canonicalize_name(requirement.name)
        for earlier_at, earlier_text, earlier, earlier_owner, earlier_choice in entries[:later]:
            exclusive = owner is not None and owner == earlier_owner and choice != earlier_choice
            if canonicalize_name(earlier.name) != name or exclusive:
                continue
            if (reason := rewriting.refusal([(earlier_text, earlier), (text, requirement)])) is not None:
                line = catalog.document.line(earlier_at)
                message = f"{text!r} can't be merged with {earlier_text!r} on line {line}, since {name} {reason}."
                found.append(catalog.document.problem(at, message, rewriting.MERGE_FIX.format(name=name), "FR002"))
                break
    return found


def _unmarked_env(root: Path, catalog: Blueprint, tracked: set[str]) -> list[Problem]:
    """FR016: each option ``env`` VAR without exactly one ``# @fastro option=`` marker directly above its line in
    ``{python_root}/.env.example``, at the VAR, and each marker line 6.1 refuses, at ``[blueprint]`` (6.1, 2.13)."""
    example = f"{catalog.info.python_root.text.rstrip('/')}/.env.example"
    reason = "isn't a tracked file" if example not in tracked else unreadable(root, example)
    text = _env_text(root, example) if reason is None else None
    markers = envfile.marked(text) if text is not None else None
    found = [
        catalog.document.problem(
            ("blueprint",),
            f"{example} {error}",
            f"Fix the marker on line {error.number} of {example}, or remove it.",
            "FR016",
        )
        for error in (envfile.refused_lines(text, catalog) if text is not None else [])
    ]
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
                elif markers is None and reason is None:
                    message = f"has no {marker} marker, since {example} isn't UTF-8 text."
                    fix = f"Save {example} as UTF-8."
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


def _env_text(root: Path, file: str) -> str | None:
    """Lint's reading of ``file`` through :func:`env_text`.

    Raises:
        Unreadable: the system won't let lint open it.
    """
    try:
        return env_text(root, file)
    except OSError as error:
        raise Unreadable(root / file, error) from None


def env_text(root: Path, file: str) -> str | None:
    """``file``, an ``.env.example``, as UTF-8 text past a byte order mark, or ``None`` when it isn't UTF-8 (1.9).
    Lint and verify both read ``{python_root}/.env.example`` through it, so they can't disagree.

    Raises:
        OSError: the system won't let fastro open it.
    """
    try:
        return (root / file).read_bytes().removeprefix(codecs.BOM_UTF8).decode("utf-8")
    except UnicodeDecodeError:
        return None
