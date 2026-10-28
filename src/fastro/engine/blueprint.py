"""``blueprint.toml``: its sections 2.1 to 2.5 loaded into frozen dataclasses (spec section 2)."""

from __future__ import annotations

import codecs
import difflib
import re
import sys
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, TypeVar

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine import conditions, spec
from fastro.engine.conditions import Condition
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Document, Problem, key_name, read
from fastro.engine.spec import Ref, RepoPath, SpecError

T = TypeVar("T")

ENV_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TOP_LEVEL = {
    "blueprint",
    "presets",
    "verify",
    "points",
    "features",
    "core",
    "wiring",
    "wiring_defaults",
    "generators",
    "commands",
    "plugin_api",
    "checks",
}
BLUEPRINT_KEYS = {
    "name",
    "version",
    "schema",
    "requires_fastro",
    "python_root",
    "import_root",
    "blueprint_only",
    "overlay",
    "extends",
    "dependencies",
}
FEATURE_KEYS = {
    "summary",
    "group",
    "requires",
    "paths",
    "dependencies",
    "dev_dependencies",
    "services",
    "contributes",
    "options",
    "when",
}
WHEN_KEYS = {"if", "paths", "contributes", "dependencies", "dev_dependencies", "services"}
OPTION_KEYS = {"choices", "default", "env", "services", "dependencies"}
CONTRIBUTION_TARGETS = ("ref", "value", "data")
CONTRIBUTION_KEYS = {"order", "if", "call"}
SCHEMA = 1
UPGRADE = "Upgrade fastro (uv tool upgrade fastro), then run this again."
COMMAND = "python -m fastro.engine.blueprint"
NOT_YET = ("wiring", "wiring_defaults", "generators", "commands", "plugin_api", "checks")
CROSS_REFERENCES_CHECKED = False


@dataclass(frozen=True)
class Extends:
    """The catalog this one extends, at an exact version (2.10)."""

    name: str
    version: Version


@dataclass(frozen=True)
class Info:
    """The ``[blueprint]`` table (2.1)."""

    name: str
    version: Version
    schema: int
    requires_fastro: SpecifierSet
    python_root: RepoPath = RepoPath("backend")
    import_root: str = "src"
    blueprint_only: tuple[RepoPath, ...] = ()
    overlay: RepoPath | None = None
    extends: Extends | None = None
    dependencies: tuple[Requirement, ...] = ()
    dev_dependencies: tuple[Requirement, ...] = ()


@dataclass(frozen=True)
class Contribution:
    """One entry contributed to a point (1.5): a bare string, or a table's parsed ``ref``, ``value`` or ``data``.

    ``via`` is the key the target came from, or ``""`` for a bare string.
    """

    target: str | Ref | Mapping[str, Any]
    via: str
    order: int | None = None
    condition: Condition | None = None
    call: bool = False
    fields: Mapping[str, str | int | bool] = field(default_factory=dict)
    at: KeyPath = ()


Contributions = Mapping[str, tuple[Contribution, ...]]


@dataclass(frozen=True)
class Check:
    """An extra check ``blueprint verify`` runs (2.2)."""

    name: str
    run: tuple[str, ...]
    cwd: RepoPath | None
    condition: Condition | None
    at: KeyPath


@dataclass(frozen=True)
class Verify:
    """The ``[verify]`` table (2.2)."""

    include_presets: bool = True
    sets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    checks: tuple[Check, ...] = ()


@dataclass(frozen=True)
class Point:
    """A declared extension point (2.3)."""

    name: str
    cardinality: str
    doc: str
    at: KeyPath


@dataclass(frozen=True)
class Option:
    """A feature's option (2.5)."""

    name: str
    choices: tuple[str, ...]
    default: str
    env: Mapping[str, str]
    services: Mapping[str, tuple[str, ...]]
    dependencies: Mapping[str, tuple[Requirement, ...]]
    at: KeyPath


@dataclass(frozen=True)
class When:
    """A feature's conditional extras (2.4)."""

    condition: Condition
    paths: tuple[RepoPath, ...]
    dependencies: tuple[Requirement, ...]
    dev_dependencies: tuple[Requirement, ...]
    services: tuple[str, ...]
    contributes: Contributions
    at: KeyPath


@dataclass(frozen=True)
class Feature:
    """A feature (2.4)."""

    name: str
    summary: str
    group: str
    requires: tuple[str, ...]
    paths: tuple[RepoPath, ...]
    dependencies: tuple[Requirement, ...]
    dev_dependencies: tuple[Requirement, ...]
    services: tuple[str, ...]
    contributes: Contributions
    options: Mapping[str, Option]
    when: tuple[When, ...]
    at: KeyPath


@dataclass(frozen=True)
class Blueprint:
    """A loaded ``blueprint.toml``, with the document it came from for placing later problems."""

    info: Info
    presets: Mapping[str, tuple[str, ...]]
    verify: Verify
    points: Mapping[str, Point]
    features: tuple[Feature, ...]
    core: Contributions
    document: Document = field(compare=False, repr=False)


class IncompatibleCatalog(CatalogError):
    """A catalog whose ``schema`` or ``requires_fastro`` rules out the running fastro."""


def load(file: str, text: str, tool: Version) -> Blueprint:
    """``text`` as the ``blueprint.toml`` named ``file``, or a :class:`CatalogError` naming every problem in it.

    A catalog whose ``schema`` or ``requires_fastro`` rules out ``tool`` is refused with only that problem.
    """
    document = read(file, text)
    reader = _Reader(document)
    if not reader.gate(document.data, tool):
        raise IncompatibleCatalog(reader.problems)
    blueprint = reader.blueprint(document.data)
    if reader.problems or blueprint is None:
        raise CatalogError(reader.problems)
    return blueprint


def _kind(value: object) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a float"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a table"
    return "a date or time"


class _Reader:
    """Reads tables key by key, collecting a problem for each key that breaks its rule.

    Each accessor takes the table's path, the table and a key, and returns ``None`` when the key is
    absent or broken.
    """

    def __init__(self, document: Document) -> None:
        self.document = document
        self.problems: list[Problem] = []

    def fail(self, path: KeyPath, message: str, fix: str = "") -> None:
        self.problems.append(self.document.problem(path, message, fix))

    def wrong(self, path: KeyPath, expected: str, value: object) -> None:
        self.fail(path, f"should be {expected}, not {_kind(value)}.")

    def parse(self, path: KeyPath, text: str, parser: Callable[[str], T]) -> T | None:
        try:
            return parser(text)
        except SpecError as error:
            self.fail(path, str(error))
            return None

    def table(
        self, path: KeyPath, value: object, keys: Collection[str] | None = None, required: Collection[str] = ()
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.wrong(path, "a table", value)
            return None
        for key in value:
            if keys is not None and key not in keys:
                self.unknown(path, key, keys)
        for key in sorted(required):
            if key not in value:
                self.fail((*path, key), "is required.", f"Add {key} to [{key_name(path)}].")
        return value

    def unknown(self, path: KeyPath, key: str, keys: Collection[str]) -> None:
        close = difflib.get_close_matches(key, keys, n=1)
        where = f"[{key_name(path)}]"
        fix = f"Did you mean '{close[0]}'?" if close else f"The keys of {where} are {', '.join(sorted(keys))}."
        self.fail((*path, key), f"isn't a key of {where}." if path else "isn't a top-level table.", fix)

    def subtable(self, path: KeyPath, table: dict[str, Any], key: str, keys: Collection[str] | None = None) -> dict[str, Any]:
        return self.table((*path, key), table[key], keys) or {} if key in table else {}

    def tables(self, path: KeyPath, table: dict[str, Any], key: str) -> list[tuple[KeyPath, dict[str, Any]]]:
        value = table.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            self.wrong((*path, key), "a list of tables", value)
            return []
        return [((*path, key, index), item) for index, item in enumerate(value)]

    def named(
        self, path: KeyPath, table: dict[str, Any], key: str, rule: Callable[[str], str]
    ) -> list[tuple[KeyPath, str, Any]]:
        """The entries of a table keyed by names, minus those whose name breaks ``rule``."""
        entries = self.subtable(path, table, key)
        at = (*path, key)
        return [((*at, name), name, item) for name, item in entries.items() if self.parse((*at, name), name, rule) is not None]

    def string(self, path: KeyPath, value: object, parser: Callable[[str], T]) -> T | None:
        if not isinstance(value, str):
            self.wrong(path, "a string", value)
            return None
        return self.parse(path, value, parser)

    def text(self, path: KeyPath, table: dict[str, Any], key: str, parser: Callable[[str], T]) -> T | None:
        return self.string((*path, key), table[key], parser) if key in table else None

    def texts(self, path: KeyPath, table: dict[str, Any], key: str, parser: Callable[[str], T]) -> tuple[T, ...]:
        value = table.get(key, [])
        if not isinstance(value, list):
            self.wrong((*path, key), "a list of strings", value)
            return ()
        items = [self.string((*path, key, index), item, parser) for index, item in enumerate(value)]
        return tuple(item for item in items if item is not None)

    def flag(self, path: KeyPath, table: dict[str, Any], key: str) -> bool | None:
        if key in table and not isinstance(table[key], bool):
            self.wrong((*path, key), "true or false", table[key])
            return None
        return table.get(key)

    def number(self, path: KeyPath, table: dict[str, Any], key: str) -> int | None:
        if key in table and (isinstance(table[key], bool) or not isinstance(table[key], int)):
            self.wrong((*path, key), "an integer", table[key])
            return None
        return table.get(key)

    def gate(self, data: dict[str, Any], tool: Version) -> bool:
        """Whether a valid ``schema`` and ``requires_fastro`` let ``tool`` read the catalog, reporting each that doesn't."""
        table = data.get("blueprint")
        if not isinstance(table, dict):
            return True
        path = ("blueprint",)
        schema = table.get("schema")
        if isinstance(schema, int) and not isinstance(schema, bool) and schema > SCHEMA:
            self.fail((*path, "schema"), f"is {schema}, and this fastro reads schema {SCHEMA}.", UPGRADE)
        elif isinstance(schema, int) and not isinstance(schema, bool) and schema < SCHEMA:
            self.fail((*path, "schema"), f"should be {SCHEMA}, not {schema}.", f"Set schema = {SCHEMA} in [blueprint].")
        requires = table.get("requires_fastro")
        try:
            allowed = _requires(requires) if isinstance(requires, str) else None
        except SpecError:
            allowed = None
        if allowed is not None and not spec.tool_allowed(allowed, tool):
            fix = (
                UPGRADE
                if _too_old(allowed, tool)
                else f"Install a fastro it accepts (uv tool install 'fastro{requires}'), then run this again."
            )
            self.fail((*path, "requires_fastro"), f"is {requires!r}, which doesn't include this fastro, {tool}.", fix)
        return not self.problems

    def blueprint(self, data: dict[str, Any]) -> Blueprint | None:
        self.table((), data, TOP_LEVEL)
        if "blueprint" not in data:
            self.fail(("blueprint",), "is required.", "Add a [blueprint] table with name, version, schema and requires_fastro.")
            return None
        info = self.info(("blueprint",), data["blueprint"])
        presets = {
            name: self.texts(("presets",), data["presets"], name, spec.feature_name)
            for _, name, _ in self.named((), data, "presets", lambda text: spec.label(text, "preset"))
        }
        points = [self.point(path, name, item) for path, name, item in self.named((), data, "points", spec.option_name)]
        features = [self.feature(path, name, item) for path, name, item in self.named((), data, "features", spec.feature_name)]
        core = self.subtable((), data, "core", {"contributes"})
        verify = self.verify(("verify",), self.subtable((), data, "verify", {"include_presets", "sets", "checks"}))
        if info is None:
            return None
        return Blueprint(
            info=info,
            presets=presets,
            verify=verify,
            points={point.name: point for point in points if point is not None},
            features=tuple(feature for feature in features if feature is not None),
            core=self.contributes(("core",), core),
            document=self.document,
        )

    def info(self, path: KeyPath, value: object) -> Info | None:
        table = self.table(path, value, BLUEPRINT_KEYS, ("name", "version", "schema", "requires_fastro"))
        if table is None:
            return None
        self.number(path, table, "schema")
        dependencies = self.subtable(path, table, "dependencies", {"runtime", "dev"})
        at_dependencies = (*path, "dependencies")
        info = Info(
            name=self.text(path, table, "name", spec.blueprint_name) or "",
            version=self.text(path, table, "version", spec.version) or Version("0"),
            schema=SCHEMA,
            requires_fastro=self.text(path, table, "requires_fastro", _requires) or SpecifierSet(),
            python_root=self.text(path, table, "python_root", spec.repo_path) or RepoPath("backend"),
            import_root=self.text(path, table, "import_root", _package) or "src",
            blueprint_only=self.texts(path, table, "blueprint_only", spec.repo_path),
            overlay=self.text(path, table, "overlay", spec.repo_path),
            extends=self.extends((*path, "extends"), table["extends"]) if "extends" in table else None,
            dependencies=self.texts(at_dependencies, dependencies, "runtime", spec.requirement),
            dev_dependencies=self.texts(at_dependencies, dependencies, "dev", spec.requirement),
        )
        return info

    def extends(self, path: KeyPath, value: object) -> Extends | None:
        table = self.table(path, value, ("name", "version"), ("name", "version"))
        if table is None:
            return None
        name = self.text(path, table, "name", spec.blueprint_name)
        version = self.text(path, table, "version", spec.version)
        return Extends(name, version) if name is not None and version is not None else None

    def verify(self, path: KeyPath, table: dict[str, Any]) -> Verify:
        sets = {}
        for at, name, _ in self.named(path, table, "sets", lambda text: spec.label(text, "verify set")):
            if name == "everything":
                self.fail(at, "is always there, as the set of every feature.", "Remove it from [verify.sets].")
            else:
                sets[name] = self.texts((*path, "sets"), table["sets"], name, spec.feature_name)
        checks = [self.check(at, item) for at, item in self.tables(path, table, "checks")]
        include = self.flag(path, table, "include_presets")
        return Verify(include is not False, sets, tuple(check for check in checks if check is not None))

    def check(self, path: KeyPath, value: dict[str, Any]) -> Check | None:
        table = self.table(path, value, ("name", "run", "cwd", "if"), ("name", "run")) or {}
        name = self.text(path, table, "name", str)
        run = self.texts(path, table, "run", str)
        if table.get("run") == []:
            self.fail((*path, "run"), 'should name the command to run, such as ["npm", "run", "build"].')
        cwd = self.text(path, table, "cwd", spec.repo_path)
        condition = self.text(path, table, "if", conditions.parse)
        return Check(name, run, cwd, condition, path) if name is not None and run else None

    def point(self, path: KeyPath, name: str, value: object) -> Point | None:
        table = self.table(path, value, ("cardinality", "doc"), ("cardinality",))
        if table is None:
            return None
        cardinality = self.text(path, table, "cardinality", _cardinality)
        doc = self.text(path, table, "doc", str)
        return Point(name, cardinality, doc or "", path) if cardinality is not None else None

    def feature(self, path: KeyPath, name: str, value: object) -> Feature | None:
        table = self.table(path, value, FEATURE_KEYS, ("summary",))
        if table is None:
            return None
        summary = self.text(path, table, "summary", str)
        group = self.text(path, table, "group", str)
        options = [self.option(at, option, item) for at, option, item in self.named(path, table, "options", spec.option_name)]
        when = [self.when(at, item) for at, item in self.tables(path, table, "when")]
        feature = Feature(
            name=name,
            summary=summary or "",
            group=group or "",
            requires=self.texts(path, table, "requires", spec.feature_name),
            paths=self.texts(path, table, "paths", spec.repo_path),
            dependencies=self.texts(path, table, "dependencies", spec.requirement),
            dev_dependencies=self.texts(path, table, "dev_dependencies", spec.requirement),
            services=self.texts(path, table, "services", _service),
            contributes=self.contributes(path, table),
            options={option.name: option for option in options if option is not None},
            when=tuple(item for item in when if item is not None),
            at=path,
        )
        return feature if summary is not None else None

    def option(self, path: KeyPath, name: str, value: object) -> Option | None:
        table = self.table(path, value, OPTION_KEYS, ("choices", "default"))
        if table is None:
            return None
        choices = self.choices(path, table)
        listed = tuple(choice for choice in table.get("choices", []) if isinstance(choice, str)) if choices else ()
        member = _in(listed) if listed else str
        default = self.text(path, table, "default", member)
        env = {
            var: text
            for _, var, _ in self.named(path, table, "env", _env_var)
            if (text := self.text((*path, "env"), table["env"], var, str)) is not None
        }
        services = {
            choice: self.texts((*path, "services"), table["services"], choice, _service)
            for _, choice, _ in self.named(path, table, "services", member)
        }
        dependencies = {
            choice: self.texts((*path, "dependencies"), table["dependencies"], choice, spec.requirement)
            for _, choice, _ in self.named(path, table, "dependencies", member)
        }
        if not choices or default not in choices:
            return None
        return Option(name, choices, default, env, services, dependencies, path)

    def choices(self, path: KeyPath, table: dict[str, Any]) -> tuple[str, ...]:
        choices = self.texts(path, table, "choices", spec.choice)
        listed = table.get("choices")
        if listed == []:
            self.fail((*path, "choices"), "should list at least one choice.")
        listed = listed if isinstance(listed, list) else []
        for index, choice in enumerate(listed):
            if choice in listed[:index]:
                self.fail((*path, "choices", index), f"{choice!r} is listed twice.")
        return tuple(dict.fromkeys(choices))

    def when(self, path: KeyPath, value: dict[str, Any]) -> When | None:
        table = self.table(path, value, WHEN_KEYS, ("if",)) or {}
        condition = self.text(path, table, "if", conditions.parse)
        paths = self.texts(path, table, "paths", spec.repo_path)
        dependencies = self.texts(path, table, "dependencies", spec.requirement)
        dev_dependencies = self.texts(path, table, "dev_dependencies", spec.requirement)
        services = self.texts(path, table, "services", _service)
        contributes = self.contributes(path, table)
        if condition is None:
            return None
        return When(condition, paths, dependencies, dev_dependencies, services, contributes, path)

    def contributes(self, path: KeyPath, table: dict[str, Any]) -> Contributions:
        contributes: dict[str, tuple[Contribution, ...]] = {}
        for at, point, items in self.named(path, table, "contributes", spec.option_name):
            if not isinstance(items, list):
                self.wrong(at, "a list of contributions", items)
                continue
            found = [self.contribution((*at, index), item) for index, item in enumerate(items)]
            contributes[point] = tuple(item for item in found if item is not None)
        return contributes

    def contribution(self, path: KeyPath, value: object) -> Contribution | None:
        if isinstance(value, str):
            return Contribution(value, "", at=path)
        if not isinstance(value, dict):
            self.wrong(path, "a ref string or a table", value)
            return None
        targets = [key for key in CONTRIBUTION_TARGETS if key in value]
        if len(targets) != 1:
            self.fail(path, "takes only one of ref, value and data." if targets else "needs one of ref, value and data.")
            return None
        via = targets[0]
        parser: Callable[[str], str | Ref] = Ref.parse if via == "ref" else str
        target = self.subtable(path, value, via) if via == "data" else self.text(path, value, via, parser)
        order = self.number(path, value, "order")
        condition = self.text(path, value, "if", conditions.parse)
        call = self.flag(path, value, "call")
        fields = {key: item for key, item in value.items() if key not in CONTRIBUTION_KEYS and key != via}
        for key, item in fields.items():
            if not isinstance(item, str | int):
                self.wrong((*path, key), "a string, an integer or true or false", item)
        if target is None or (via == "data" and not isinstance(value[via], dict)):
            return None
        return Contribution(target, via, order, condition, call is True, fields, path)


def _requires(text: str) -> SpecifierSet:
    allowed = spec.specifier(text)
    if any(bound.operator == "===" for bound in allowed):
        raise SpecError(
            f"{text!r} uses '===', which can't express a version range: "
            "write it with ==, >= and <, such as '>=0.1.0.dev0,<0.2'."
        )
    if not allowed:
        raise SpecError("should name the fastro versions that can read this catalog, such as '>=0.1.0.dev0,<0.2'.")
    return allowed


def _too_old(allowed: SpecifierSet, tool: Version) -> bool:
    """Whether ``tool`` falls below a lower bound of ``allowed``, so a newer fastro is what's needed."""
    for bound in allowed:
        if bound.contains(tool, prereleases=True):
            continue
        if bound.operator in (">", ">="):
            return True
        if bound.operator in ("==", "~=") and tool < Version(bound.version.removesuffix(".*")):
            return True
    return False


def _package(text: str) -> str:
    if not spec.identifier(text):
        raise SpecError(f"{text!r} isn't an importable package name, such as 'src'.")
    return text


def _service(text: str) -> str:
    if not text:
        raise SpecError("should name a compose service, not be empty.")
    return text


def _env_var(text: str) -> str:
    if not ENV_VAR.fullmatch(text):
        raise SpecError(
            f"{text!r} isn't an environment variable name: use letters, digits and underscores, not starting with a digit."
        )
    return text


def _cardinality(text: str) -> str:
    if text not in ("many", "one"):
        raise SpecError(f"should be 'many' or 'one', not {text!r}.")
    return text


def _not_a_choice(text: str, choices: tuple[str, ...]) -> str:
    return f"{text!r} isn't one of the choices: {', '.join(map(repr, choices))}."


def _in(choices: tuple[str, ...]) -> Callable[[str], str]:
    def rule(text: str) -> str:
        if text not in choices:
            raise SpecError(_not_a_choice(text, choices))
        return text

    return rule


def main(argv: list[str]) -> int:
    """Check the ``blueprint.toml`` at ``argv[0]``, a repo or the file itself, printing ``ok`` or its problems."""
    if len(argv) != 1:
        print(f"usage: {COMMAND} PATH", file=sys.stderr)
        return 2
    path = Path(argv[0])
    file = path / "blueprint.toml" if path.is_dir() else path
    if not file.is_file():
        why = "has no blueprint.toml" if path.is_dir() else "doesn't exist"
        print(f"error: {path} {why}. Pass a blueprint repo or its blueprint.toml: {COMMAND} PATH", file=sys.stderr)
        return 2
    try:
        tool = Version(metadata.version("fastro"))
    except metadata.PackageNotFoundError:
        print(
            "error: fastro isn't installed, so this can't tell which catalogs it reads. "
            "Install it (uv pip install -e PATH_TO_FASTRO), then run this again.",
            file=sys.stderr,
        )
        return 3
    try:
        loaded = load(str(file), _text(file), tool)
    except OSError as error:
        print(
            f"error: can't read {file}: {error.strerror}. Check its permissions, then run {COMMAND} {path} again.",
            file=sys.stderr,
        )
        return 3
    except CatalogError as error:
        for problem in error.problems:
            print(problem)
        count = len(error.problems)
        noun, them = ("problem", "it") if count == 1 else ("problems", "them")
        print(f"{count} {noun} in {file}. Fix {them}, then run {COMMAND} {path} again.")
        return 3 if isinstance(error, IncompatibleCatalog) else 1
    info = loaded.info
    features, points, presets = (
        _counted(len(loaded.features), "feature"),
        _counted(len(loaded.points), "point"),
        _counted(len(loaded.presets), "preset"),
    )
    print(f"ok: {file} is {info.name} {info.version}, with {features}, {points} and {presets}.")
    if line := not_checked():
        print(line)
    return 0


def not_checked() -> str:
    """The line saying what ``main`` can't vouch for yet, built from :data:`NOT_YET`; empty once it's all checked."""
    sections = [f"[[{name}]]" if name == "wiring" else f"[{name}]" for name in NOT_YET]
    parts = [f"what's inside {_listed(sections)}"] if sections else []
    if not CROSS_REFERENCES_CHECKED:
        parts.append("names that refer to another section (features, points and options)")
    return f"Not checked yet: {', and '.join(parts)}." if parts else ""


def _text(file: Path) -> str:
    data = file.read_bytes().removeprefix(codecs.BOM_UTF8)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        line = data[: error.start].count(b"\n") + 1
        raise CatalogError(
            [Problem(str(file), line, "", "isn't UTF-8 text.", "Save it as UTF-8, then run this again.")]
        ) from None


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
