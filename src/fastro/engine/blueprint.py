"""``blueprint.toml``: its sections 2.1 to 2.5 loaded into frozen dataclasses (spec section 2)."""

from __future__ import annotations

import ast
import builtins
import difflib
import ipaddress
import math
import re
import string
import sys
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine import conditions, references, spec
from fastro.engine.conditions import Condition
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Document, Problem, read
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
    "commands",
}
WHEN_KEYS = {"if", "paths", "contributes", "dependencies", "dev_dependencies", "services"}
OPTION_KEYS = {"choices", "default", "env", "services", "dependencies"}
SLOT_KEYS = {
    "tuple": ("name", "point", "annotation", "item", "defaults", "literal", "item_imports"),
    "list": ("name", "point", "annotation", "item", "defaults", "literal", "item_imports"),
    "value": ("name", "point", "annotation", "fallback", "fallback_imports"),
    "class": ("name", "point", "fallback", "append", "doc"),
    "reexport": ("name", "point", "fallback"),
    "raw": ("code",),
    "data": ("name", "point", "format"),
}
SLOT_REQUIRED = {
    "tuple": ("name", "point", "annotation"),
    "list": ("name", "point", "annotation"),
    "value": ("name", "point", "annotation"),
    "class": ("name", "point", "doc"),
    "reexport": ("name", "point"),
    "raw": ("code",),
    "data": ("name", "point", "format"),
}
HEADER_WIDTH = 100
BUILTINS = frozenset(dir(builtins))
FIELD = "__fastro_field__"
IN_IMPORTS = "Import it in the file's imports, or define it in an earlier slot."
IN_FALLBACK_IMPORTS = "Import it in the file's imports or the slot's fallback_imports, or define it in an earlier slot."
IN_ITEM_IMPORTS = "Import it in the file's imports or the slot's item_imports, or define it in an earlier slot."
LINE_LENGTH = 88
LINE_LENGTHS = range(1, 321)
PARAM_TYPES = ("bool", "choice", "cidr", "int", "str")
MODULE_GENERATOR = "module"
TEMPLATE_FIELDS = {"name": "sample_name", "name_dashed": "sample-name", "Name": "SampleName"}
CONTRIBUTION_TARGETS = ("ref", "value", "data")
CONTRIBUTION_KEYS = {"order", "if", "call"}
DATA_CONTRIBUTION_KEYS = ("data", "order", "if")
SCHEMA = 1
UPGRADE = "Upgrade fastro (uv tool upgrade fastro), then run this again."
KEPT = (RepoPath("blueprint.toml"), RepoPath("fastro.toml"), RepoPath(".fastro/"))


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

    @property
    def all_blueprint_only(self) -> tuple[RepoPath, ...]:
        """``blueprint_only`` with what every blueprint keeps to itself (2.1) added after the author's own: the kept
        files, and the overlay directory, whose files reach projects only through the overlay."""
        kept = [*KEPT, RepoPath(self.overlay.text.rstrip("/") + "/")] if self.overlay is not None else list(KEPT)
        return (*self.blueprint_only, *(path for path in kept if path not in self.blueprint_only))


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
    app: str | None = None


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
    condition_text: str = ""


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
    commands: Mapping[tuple[str, ...], Command]
    at: KeyPath


@dataclass(frozen=True)
class Slot:
    """One slot of a generated file (2.6): what it's called, which point it collects and how it renders."""

    kind: str
    name: str = ""
    point: str = ""
    annotation: str = ""
    item: str = ""
    defaults: Mapping[str, str | int | bool] = field(default_factory=dict)
    literal: bool = False
    item_imports: tuple[str, ...] = ()
    fallback: str | Ref | None = None
    fallback_imports: tuple[str, ...] = ()
    append: tuple[Ref, ...] = ()
    doc: str = ""
    code: str = ""
    format: str = ""
    at: KeyPath = ()


@dataclass(frozen=True)
class Wiring:
    """A file the engine generates (2.6)."""

    path: RepoPath
    doc: str
    condition: Condition | None
    imports: tuple[str, ...]
    slots: tuple[Slot, ...]
    at: KeyPath
    condition_text: str = ""


@dataclass(frozen=True)
class Prefix:
    """The widest network a cidr param accepts, as the shortest prefix length allowed in each address family (2.9)."""

    v4: int
    v6: int

    def limit(self, version: int) -> int:
        return self.v4 if version == 4 else self.v6


@dataclass(frozen=True)
class Param:
    """A generator's parameter (2.9)."""

    name: str
    type: str
    default: str | int | bool
    help: str = ""
    choices: tuple[str, ...] = ()
    private_only: bool = False
    max_prefix: Prefix | None = None
    at: KeyPath = ()


@dataclass(frozen=True)
class Register:
    """What ``make module`` adds under ``[app]``, as templates over ``{name}``, ``{name_dashed}`` and ``{Name}`` (2.9)."""

    paths: tuple[str, ...]
    contributes: Mapping[str, tuple[str | Mapping[str, Any], ...]]
    parsed: Contributions = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Generator:
    """A ``fastro make`` generator (2.9)."""

    name: str
    templates: RepoPath
    params: Mapping[str, Param]
    register: Register | None
    types: Mapping[str, Mapping[str, Any]]
    at: KeyPath
    modes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Command:
    """A command the blueprint declares, run in the project (2.9)."""

    words: tuple[str, ...]
    module: str
    cwd: RepoPath | None = None
    help: str = ""
    outside_project: str = "error"
    at: KeyPath = ()


@dataclass(frozen=True)
class PluginApi:
    """What plugins may contribute to and import (2.11)."""

    version: int
    points: tuple[str, ...]
    public: tuple[str, ...]
    public_with: Mapping[str, tuple[str, ...]]
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
    wiring: tuple[Wiring, ...]
    header: str
    line_length: int
    generators: Mapping[str, Generator]
    commands: Mapping[tuple[str, ...], Command]
    plugin_api: PluginApi | None
    checks: Mapping[str, Mapping[str, Any]]
    toolchain_env: Mapping[str, str]
    document: Document = field(compare=False, repr=False)


class IncompatibleCatalog(CatalogError):
    """A catalog whose ``schema`` or ``requires_fastro`` rules out the running fastro."""


class CrossReferenceError(CatalogError):
    """A catalog whose keys all read cleanly, with names that refer to something it doesn't declare."""


class ExtendingCatalogError(CatalogError):
    """Key problems in a catalog that extends another, whose names wait for the merge with its base."""


def load(file: str, text: str, tool: Version) -> Blueprint:
    """``text`` as the ``blueprint.toml`` named ``file``, or a :class:`CatalogError` naming every problem in it.

    A catalog whose ``schema`` or ``requires_fastro`` rules out ``tool`` is refused with only that problem. Names
    that refer to another section are checked once every key reads cleanly, unless the catalog extends another.
    """
    document = read(file, text)
    reader = Reader(document)
    if not reader.gate(document.data, tool):
        raise IncompatibleCatalog(reader.problems)
    blueprint = reader.blueprint(document.data)
    if reader.problems or blueprint is None:
        info = document.data.get("blueprint")
        extends = isinstance(info, dict) and "extends" in info
        raise (ExtendingCatalogError if extends else CatalogError)(reader.problems)
    if blueprint.info.extends is None and (found := references.problems(blueprint)):
        raise CrossReferenceError(found)
    return blueprint


def readable(text: str, tool: Version) -> bool:
    """Whether ``text`` reads as a ``blueprint.toml`` whose ``schema`` and ``requires_fastro`` let ``tool`` read it,
    without checking anything else (spec 1.7: how a source with no ref picks its release)."""
    try:
        document = read("blueprint.toml", text)
    except CatalogError:
        return False
    table = document.data.get("blueprint")
    requires = table.get("requires_fastro") if isinstance(table, dict) else None
    try:
        allowed = _requires(requires) if isinstance(requires, str) else None
    except SpecError:
        return False
    return allowed is not None and Reader(document).gate(document.data, tool)


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


class Reader:
    """Reads tables key by key, collecting a problem for each key that breaks its rule.

    Each accessor takes the table's path, the table and a key, and returns ``None`` when the key is
    absent or broken.
    """

    top = ""

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
                self.fail((*path, key), "is required.", f"Add {key} to {self.document.table(path)}.")
        return value

    def unknown(self, path: KeyPath, key: str, keys: Collection[str], where: str = "") -> None:
        close = difflib.get_close_matches(key, keys, n=1)
        if not path and self.top:
            fix = f"Did you mean '{close[0]}'?" if close else f"The top-level keys are {', '.join(sorted(keys))}."
            self.fail((key,), f"isn't a top-level key of {self.top}.", fix)
            return
        where = where or self.document.table(path)
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

    def repeats(self, path: KeyPath, value: object) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                if item in value[:index]:
                    self.fail((*path, index), f"{item!r} is listed twice.")

    def options(self, path: KeyPath, table: dict[str, Any]) -> dict[tuple[str, str], str]:
        options: dict[tuple[str, str], str] = {}
        for key, value in table.items():
            if isinstance(value, dict):
                example = f'"{key}.{next(iter(value), "option")}" = "{_first(value)}"'
                self.fail((*path, key), "should be a string, not a table.", f"Quote the whole key, such as {example}.")
                continue
            pair = self.parse((*path, key), key, spec.option_key)
            chosen = self.string((*path, key), value, str)
            if pair is not None and chosen is not None:
                options[pair] = chosen
        return options

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
        verify_table = self.subtable((), data, "verify", {"include_presets", "sets", "checks", "app"})
        verify = self.verify(("verify",), verify_table, info.import_root if info is not None else None)
        python_root = info.python_root if info is not None else RepoPath("backend")
        wiring = [self.wiring(at, item, python_root) for at, item in self.tables((), data, "wiring")]
        self.duplicates(
            [(file.at, file.path.text) for file in wiring if file is not None], "path", "is generated by {} already."
        )
        self.nested([(file.at, file.path.text) for file in wiring if file is not None])
        defaults = self.subtable((), data, "wiring_defaults", {"header", "line_length"})
        line_length = self.number(("wiring_defaults",), defaults, "line_length")
        width = LINE_LENGTH if "line_length" not in defaults else line_length
        if width is not None and width not in LINE_LENGTHS:
            self.fail(("wiring_defaults", "line_length"), f"should be from 1 to 320, not {width}.")
            width = None
        header = self.text(("wiring_defaults",), defaults, "header", lambda text: _header(text, width or HEADER_WIDTH))
        generators = [
            self.generator(at, name, item) for at, name, item in self.named((), data, "generators", spec.generator_name)
        ]
        commands = [self.command(at, name, item) for at, name, item in self.named((), data, "commands", spec.command_words)]
        plugin_api = self.plugin_api(("plugin_api",), data["plugin_api"]) if "plugin_api" in data else None
        checks, toolchain_env = self.checks(("checks",), self.subtable((), data, "checks"))
        declared = [command for command in commands if command is not None] + [
            command for feature in features if feature is not None for command in feature.commands.values()
        ]
        declared.sort(key=lambda command: self.document.line(command.at))
        self.duplicates([(command.at, " ".join(command.words)) for command in declared], None, "is declared by {} already.")
        if info is None:
            return None
        return Blueprint(
            info=info,
            presets=presets,
            verify=verify,
            points={point.name: point for point in points if point is not None},
            features=tuple(feature for feature in features if feature is not None),
            core=self.contributes(("core",), core),
            wiring=tuple(file for file in wiring if file is not None),
            header=header or "",
            line_length=width or LINE_LENGTH,
            generators={generator.name: generator for generator in generators if generator is not None},
            commands={command.words: command for command in commands if command is not None},
            plugin_api=plugin_api,
            checks=checks,
            toolchain_env=toolchain_env,
            document=self.document,
        )

    def duplicates(self, entries: list[tuple[KeyPath, str]], key: str | None, message: str) -> None:
        """Report each entry whose ``key``, or whose own name when ``key`` is None, repeats an earlier entry's."""
        first: dict[str, KeyPath] = {}
        for at, value in entries:
            if value in first:
                self.fail(at if key is None else (*at, key), f"{value!r} " + message.format(self.document.key(first[value])))
            else:
                first[value] = at

    def nested(self, entries: list[tuple[KeyPath, str]]) -> None:
        """Report each wiring path that sits under, or has under it, an earlier one (spec 2.6)."""
        for index, (at, value) in enumerate(entries):
            for earlier, other in entries[:index]:
                where = self.document.key(earlier)
                if value.startswith(other + "/"):
                    relation = f"sits under {where}'s path"
                elif other.startswith(value + "/"):
                    relation = f"has {where}'s path under it"
                else:
                    continue
                self.fail((*at, "path"), f"{value!r} {relation}, and one wiring file can't be inside another.")
                break

    def wiring(self, path: KeyPath, table: dict[str, Any], python_root: RepoPath) -> Wiring | None:
        self.table(path, table, ("path", "doc", "if", "imports", "slots"), ("path", "doc", "slots"))
        file = self.text(path, table, "path", _file)
        doc = self.text(path, table, "doc", _docstring)
        condition = self.text(path, table, "if", conditions.parse)
        imports = self.texts(path, table, "imports", _import)
        found = [self.slot(at, item) for at, item in self.tables(path, table, "slots")]
        if table.get("slots") == []:
            self.fail((*path, "slots"), "should list at least one slot.")
        slots = tuple(slot for slot in found if slot is not None)
        self.duplicates([(slot.at, slot.name) for slot in slots if slot.kind != "raw"], "name", "is defined by {} already.")
        self.dunder_all(slots)
        self.bound_names(imports, slots)
        family = [(slot, slot.kind == "data") for slot in slots]
        for slot, data in family[1:]:
            if data != family[0][1]:
                kinds = "data" if family[0][1] else "Python"
                self.fail(slot.at, f"is a {slot.kind} slot in a file of {kinds} slots, and a file is all data or all Python.")
        formats = [slot for slot in slots if slot.format]
        for slot in formats[1:]:
            if slot.format != formats[0].format:
                first = formats[0].format
                self.fail(
                    (*slot.at, "format"),
                    f"is {slot.format!r}, and {self.document.key(formats[0].at)} is {first!r}: a data file's slots all "
                    "share one format.",
                    f"Give it format = {first!r}, or move it to a data file of its own.",
                )
        python = bool(family) and not family[0][1]
        if file is not None and python:
            root = python_root.text.rstrip("/")
            if not file.text.startswith(f"{root}/"):
                self.fail((*path, "path"), f"{file.text!r} isn't under python_root ({root!r}), and its slots are Python.")
            elif not file.text.endswith(".py"):
                self.fail((*path, "path"), f"{file.text!r} doesn't end in .py, and its slots are Python.")
        if file is None or doc is None or not slots:
            return None
        return Wiring(file, doc, condition, imports, slots, path, _source(table, "if"))

    def dunder_all(self, slots: tuple[Slot, ...]) -> None:
        """Refuse a second ``reexport`` slot, and ``raw`` code assigning ``__all__`` beside one: the reexport renders the
        file's ``__all__`` (2.6)."""
        reexports = [slot for slot in slots if slot.kind == "reexport"]
        if not reexports:
            return
        first = self.document.key(reexports[0].at)
        for slot in reexports[1:]:
            self.fail(
                slot.at,
                f"is a second reexport slot in this file, after {first}, and each would render the file's __all__.",
                "Keep one reexport slot in each file.",
            )
        for slot in slots:
            if _assigns_all(slot.code):
                self.fail(
                    (*slot.at, "code"),
                    f"assigns __all__, and {first}, a reexport slot, renders the file's __all__.",
                    "Drop the assignment: the reexport's __all__ lists its name.",
                )

    def bound_names(self, imports: tuple[str, ...], slots: tuple[Slot, ...]) -> None:
        """Refuse a name an annotation, a value fallback or an item template renders whatever the selection that the
        file doesn't bind whatever the selection: a builtin, its ``imports``, or an earlier slot (2.6)."""
        bound = _binds(imports)
        for slot in slots:
            if slot.kind in ("tuple", "list", "value"):
                self.unbound((*slot.at, "annotation"), slot.annotation, bound, IN_IMPORTS)
            if slot.kind == "value" and isinstance(slot.fallback, str):
                fallback = _binds(slot.fallback_imports)
                extra = None if bound is None or fallback is None else bound | fallback
                self.unbound((*slot.at, "fallback"), slot.fallback, extra, IN_FALLBACK_IMPORTS)
            if slot.item:
                items = _binds(slot.item_imports)
                extra = None if bound is None or items is None else bound | items
                self.unbound((*slot.at, "item"), _item_expression(slot.item), extra, IN_ITEM_IMPORTS)
            defined = _defines(slot)
            bound = None if bound is None or defined is None else bound | defined

    def unbound(self, path: KeyPath, expression: str, bound: frozenset[str] | None, fix: str) -> None:
        """Refuse each name ``expression`` uses that's neither a builtin nor in ``bound`` (``None``: a star import)."""
        if bound is None:
            return
        for name in _names(expression):
            if name not in bound and name not in BUILTINS and name != FIELD:
                self.fail(path, f"uses {name!r}, which this file doesn't bind whatever the selection.", fix)

    def slot(self, path: KeyPath, table: dict[str, Any]) -> Slot | None:
        if "kind" not in table:
            self.fail((*path, "kind"), "is required.", f"Add kind to {self.document.table(path)}.")
            return None
        kind = self.text(path, table, "kind", _kind_of_slot)
        if kind is None:
            return None
        keys = ("kind", *SLOT_KEYS[kind])
        for key in table:
            if key not in keys:
                self.unknown(path, key, keys, f"a {kind} slot")
        table = {key: value for key, value in table.items() if key in keys}
        for key in SLOT_REQUIRED[kind]:
            if key not in table:
                self.fail((*path, key), f"is required for a {kind} slot.", f"Add {key} to {self.document.table(path)}.")
        fallback_parser: Callable[[str], str | Ref] = _expression if kind == "value" else Ref.parse
        defaults = self.subtable(path, table, "defaults")
        for key, item in defaults.items():
            if not isinstance(item, str | int):
                self.wrong((*path, "defaults", key), "a string, an integer or true or false", item)
        return Slot(
            kind=kind,
            name=self.text(path, table, "name", _python_name) or "",
            point=self.text(path, table, "point", spec.option_name) or "",
            annotation=self.text(path, table, "annotation", _expression) or "",
            item=self.text(path, table, "item", _item) or "",
            defaults=defaults,
            literal=self.flag(path, table, "literal") is True,
            item_imports=self.texts(path, table, "item_imports", _import),
            fallback=self.text(path, table, "fallback", fallback_parser),
            fallback_imports=self.texts(path, table, "fallback_imports", _import),
            append=self.texts(path, table, "append", Ref.parse),
            doc=self.text(path, table, "doc", _docstring) or "",
            code=self.text(path, table, "code", _code) or "",
            format=self.text(path, table, "format", _data_format) or "",
            at=path,
        )

    def generator(self, path: KeyPath, name: str, value: object) -> Generator | None:
        table = self.table(path, value, ("templates", "modes", "params", "register", "types"), ("templates",))
        if table is None:
            return None
        templates = self.text(path, table, "templates", spec.repo_path)
        modes = self.modes(path, name, table)
        params = [self.param(at, param, item) for at, param, item in self.named(path, table, "params", spec.option_name)]
        for key in ("register", "types"):
            if key in table and name != MODULE_GENERATOR:
                self.fail((*path, key), "is only for the module generator, which make module runs.")
        module = name == MODULE_GENERATOR
        register = self.register((*path, "register"), table["register"]) if module and "register" in table else None
        types = {kind: item for at, kind, item in self.named(path, table, "types", str) if self.table(at, item) is not None}
        if templates is None:
            return None
        return Generator(
            name,
            templates,
            {param.name: param for param in params if param is not None},
            register,
            types if module else {},
            path,
            modes,
        )

    def modes(self, path: KeyPath, name: str, table: dict[str, Any]) -> tuple[str, ...]:
        if "modes" not in table:
            return ()
        if name == MODULE_GENERATOR:
            self.fail((*path, "modes"), "isn't for the module generator, whose arguments come with make module.")
            return ()
        if table["modes"] == []:
            self.fail((*path, "modes"), "should list at least one mode, or be left out for a generator without modes.")
        self.repeats((*path, "modes"), table["modes"])
        return self.texts(path, table, "modes", spec.mode_name)

    def param(self, path: KeyPath, name: str, value: object) -> Param | None:
        keys = ("type", "default", "help", "choices", "private_only", "max_prefix")
        table = self.table(path, value, keys, ("type", "default"))
        if table is None:
            return None
        kind = self.text(path, table, "type", _param_type)
        for key, only in (("choices", "choice"), ("private_only", "cidr"), ("max_prefix", "cidr")):
            if kind is not None and key in table and kind != only:
                self.fail((*path, key), f"is only for a {only} param.")
        if kind == "choice" and "choices" not in table:
            self.fail((*path, "choices"), "is required for a choice param.", f"Add choices to {self.document.table(path)}.")
        choices = self.choices(path, table) if kind == "choice" and "choices" in table else ()
        private_only = self.flag(path, table, "private_only") is True if kind == "cidr" else False
        max_prefix = (
            self.prefix((*path, "max_prefix"), table["max_prefix"]) if kind == "cidr" and "max_prefix" in table else None
        )
        default = (
            self.default(path, table, kind, choices, private_only, max_prefix)
            if kind is not None and "default" in table
            else None
        )
        if kind is None or default is None:
            return None
        return Param(name, kind, default, self.text(path, table, "help", str) or "", choices, private_only, max_prefix, path)

    def prefix(self, path: KeyPath, value: object) -> Prefix | None:
        """``max_prefix``: an integer for both families, or a table with both ``v4`` and ``v6`` (2.9)."""
        if isinstance(value, int) and not isinstance(value, bool):
            if not 0 <= value <= 128:
                self.fail(path, f"should be from 0 to 128, not {value}.")
                return None
            return Prefix(value, value)
        if not isinstance(value, dict):
            self.wrong(path, "an integer or a table with v4 and v6", value)
            return None
        table = self.table(path, value, ("v4", "v6"), ("v4", "v6")) or {}
        limits = {key: self.number(path, table, key) for key in ("v4", "v6")}
        for key, top in (("v4", 32), ("v6", 128)):
            if (limit := limits[key]) is not None and not 0 <= limit <= top:
                self.fail((*path, key), f"should be from 0 to {top}, not {limit}.")
                limits[key] = None
        v4, v6 = limits["v4"], limits["v6"]
        return Prefix(v4, v6) if v4 is not None and v6 is not None else None

    def default(
        self,
        path: KeyPath,
        table: dict[str, Any],
        kind: str,
        choices: tuple[str, ...],
        private_only: bool,
        max_prefix: Prefix | None,
    ) -> str | int | bool | None:
        if kind == "int":
            return self.number(path, table, "default")
        if kind == "bool":
            return self.flag(path, table, "default")
        if kind == "choice":
            return self.text(path, table, "default", _in(choices) if choices else str)
        if kind == "cidr":
            return self.text(path, table, "default", lambda text: _network(text, private_only, max_prefix))
        return self.text(path, table, "default", str)

    def register(self, path: KeyPath, value: object) -> Register | None:
        table = self.table(path, value, ("paths", "contributes"))
        if table is None:
            return None
        paths = self.texts(path, table, "paths", _filled(spec.repo_path))
        contributes: dict[str, tuple[str | Mapping[str, Any], ...]] = {}
        parsed: dict[str, tuple[Contribution, ...]] = {}
        for at, point, items in self.named(path, table, "contributes", spec.option_name):
            if not isinstance(items, list):
                self.wrong(at, "a list of contributions", items)
                continue
            filled = [self.templated((*at, index), item) for index, item in enumerate(items)]
            contributes[point] = tuple(items)
            parsed[point] = tuple(item for item in filled if item is not None)
        return Register(paths, contributes, parsed)

    def templated(self, path: KeyPath, value: object) -> Contribution | None:
        """Check a ``register`` contribution as it reads once its templates are filled in, with ``if`` as written."""
        if isinstance(value, str):
            text = self.string(path, value, _filled(str))
            return None if text is None else Contribution(text.format(**TEMPLATE_FIELDS), "", at=path)
        if not isinstance(value, dict):
            self.wrong(path, "a ref string or a table", value)
            return None
        filled: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and key != "if":
                if self.string((*path, key), item, _filled(Ref.parse if key == "ref" else str)) is None:
                    return None
                item = item.format(**TEMPLATE_FIELDS)
            filled[key] = item
        return self.contribution(path, filled)

    def command(self, path: KeyPath, words: str, value: object) -> Command | None:
        table = self.table(path, value, ("module", "cwd", "help", "outside_project"), ("module",))
        if table is None:
            return None
        module = self.text(path, table, "module", _module)
        cwd = self.text(path, table, "cwd", spec.repo_path)
        help_text = self.text(path, table, "help", str)
        outside = self.text(path, table, "outside_project", _outside_project)
        if module is None:
            return None
        return Command(tuple(words.split(" ")), module, cwd, help_text or "", outside or "error", path)

    def plugin_api(self, path: KeyPath, value: object) -> PluginApi | None:
        table = self.table(path, value, ("version", "points", "public", "public_with"), ("version",))
        if table is None:
            return None
        version = self.number(path, table, "version")
        if version is not None and version < 1:
            self.fail((*path, "version"), f"should be 1 or more, not {version}.")
        points = self.texts(path, table, "points", spec.option_name)
        public = self.texts(path, table, "public", _public)
        public_with = {
            feature: self.texts((*path, "public_with"), table["public_with"], feature, _public)
            for _, feature, _ in self.named(path, table, "public_with", spec.option_name)
        }
        if version is None or version < 1:
            return None
        return PluginApi(version, points, public, public_with, path)

    def checks(self, path: KeyPath, table: dict[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
        toolchain = self.subtable(path, table, "toolchain", ("env",))
        env = {
            var: text
            for _, var, _ in self.named((*path, "toolchain"), toolchain, "env", _env_var)
            if (text := self.text((*path, "toolchain", "env"), toolchain["env"], var, str)) is not None
        }
        codes = {key: item for key, item in table.items() if key != "toolchain"}
        checks = {
            code: item
            for at, code, item in self.named((), {"checks": codes}, "checks", spec.check_code)
            if self.table(at, item) is not None
        }
        return checks, env

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

    def verify(self, path: KeyPath, table: dict[str, Any], import_root: str | None) -> Verify:
        sets = {}
        for at, name, _ in self.named(path, table, "sets", lambda text: spec.label(text, "verify set")):
            if name == "everything":
                self.fail(at, "is always there, as the set of every feature.", "Remove it from [verify.sets].")
            else:
                sets[name] = self.texts((*path, "sets"), table["sets"], name, spec.feature_name)
        checks = [self.check(at, item) for at, item in self.tables(path, table, "checks")]
        include = self.flag(path, table, "include_presets")
        app = self.text(path, table, "app", _module)
        if app is not None and import_root is not None and app.split(".")[0] != import_root:
            message = f"{app!r} doesn't start with {import_root!r}, the import_root, and the app is imported from there."
            self.fail((*path, "app"), message, f"Name the app's module under {import_root}, such as '{import_root}.main'.")
        return Verify(include is not False, sets, tuple(check for check in checks if check is not None), app)

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
        commands = [
            self.command(at, words, item) for at, words, item in self.named(path, table, "commands", spec.command_words)
        ]
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
            commands={command.words: command for command in commands if command is not None},
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
            if (text := self.text((*path, "env"), table["env"], var, _env_template)) is not None
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
        return When(condition, paths, dependencies, dev_dependencies, services, contributes, path, _source(table, "if"))

    def contributes(self, path: KeyPath, table: dict[str, Any]) -> Contributions:
        contributes: dict[str, tuple[Contribution, ...]] = {}
        for at, point, items in self.named(path, table, "contributes", spec.option_name):
            if not isinstance(items, list):
                self.wrong(at, "a list of contributions", items)
                continue
            found = [self.contribution((*at, index), item) for index, item in enumerate(items)]
            contributes[point] = tuple(item for item in found if item is not None)
        return contributes

    def data(self, path: KeyPath, table: dict[str, Any]) -> None:
        """Refuse a ``feature`` key, which fastro adds to each item, and any value JSON can't hold (2.6.1)."""
        if "feature" in table:
            self.fail((*path, "feature"), "is added to each item by fastro, so a data table can't give it.", "Rename the key.")
        for key, item in table.items():
            self.json_value((*path, key), item)

    def json_value(self, path: KeyPath, value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                self.json_value((*path, key), item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self.json_value((*path, index), item)
        elif isinstance(value, float) and not math.isfinite(value):
            self.fail(path, f"is {value}, which JSON can't hold.", "Use a number, or write it as a string.")
        elif not isinstance(value, str | int | float):
            self.wrong(path, "a string, an integer, a float, true or false, a list or a table", value)

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
        if via == "data":
            for key in value:
                if key not in DATA_CONTRIBUTION_KEYS:
                    self.unknown(path, key, DATA_CONTRIBUTION_KEYS, "a data contribution")
            fields = {}
            if isinstance(value[via], dict):
                self.data((*path, via), value[via])
        for key, item in fields.items():
            if not isinstance(item, str | int):
                self.wrong((*path, key), "a string, an integer or true or false", item)
        if target is None or (via == "data" and not isinstance(value[via], dict)):
            return None
        return Contribution(target, via, order, condition, call is True, fields, path)


def _source(table: dict[str, Any], key: str) -> str:
    """The text ``table`` gives ``key``, as the catalog wrote it, or ``""`` when it doesn't give one."""
    value = table.get(key, "")
    return value if isinstance(value, str) else ""


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


def _file(text: str) -> RepoPath:
    path = spec.repo_path(text)
    if path.subtree:
        raise SpecError(f"{text!r} names a directory: a wiring file is one file.")
    return path


def _docstring(text: str) -> str:
    if '"""' in text:
        raise SpecError("can't contain '\"\"\"', which would end the docstring.")
    if "\\" in text:
        raise SpecError("can't contain a backslash, which Python would read as an escape inside the docstring.")
    if text.strip("\n").endswith('"'):
        raise SpecError("can't end in '\"', which would run into the docstring's closing '\"\"\"'.")
    return text


def _header(text: str, line_length: int = LINE_LENGTH) -> str:
    """``text`` if it fits in a docstring, with every line at most 100 characters and at most ``line_length`` (2.8)."""
    _docstring(text)
    for number, line in enumerate(text.strip("\n").split("\n"), 1):
        if len(line) > HEADER_WIDTH:
            raise SpecError(f"line {number} is {len(line)} characters long, and header lines are at most {HEADER_WIDTH}.")
        if len(line) > line_length:
            raise SpecError(f"line {number} is {len(line)} characters long, and line_length is {line_length}.")
    return text


def _import(text: str) -> str:
    try:
        body = ast.parse(text).body
    except SyntaxError:
        body = []
    if len(body) != 1 or not isinstance(body[0], ast.Import | ast.ImportFrom):
        raise SpecError(f"{text!r} isn't one import statement, such as 'from fastapi import APIRouter'.")
    return text


def _expression(text: str) -> str:
    try:
        ast.parse(text, mode="eval")
    except SyntaxError:
        raise SpecError(f"{text!r} isn't a Python expression.") from None
    return text


def _code(text: str) -> str:
    try:
        ast.parse(text)
    except SyntaxError as error:
        raise SpecError(f"isn't Python: {error.msg} on its line {error.lineno}.") from None
    return text


def _assigns_all(code: str) -> bool:
    """Whether module-level ``code`` assigns ``__all__``, by ``=``, an annotation or an augmented assignment."""
    for node in ast.parse(code).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        else:
            continue
        if any(_binds_all(target) for target in targets):
            return True
    return False


def _binds_all(target: ast.expr) -> bool:
    """Whether an assignment ``target`` binds the name ``__all__``, directly or by unpacking into it."""
    if isinstance(target, ast.Name):
        return target.id == "__all__"
    if isinstance(target, ast.Starred):
        return _binds_all(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return any(_binds_all(element) for element in target.elts)
    return False


def _binds(lines: tuple[str, ...]) -> frozenset[str] | None:
    """The names import ``lines`` bind, or ``None`` when one is a star import, which could bind any name."""
    names: set[str] = set()
    for line in lines:
        (node,) = ast.parse(line).body
        assert isinstance(node, ast.Import | ast.ImportFrom)
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            return None
        plain = isinstance(node, ast.Import)
        names.update(alias.asname or (alias.name.split(".")[0] if plain else alias.name) for alias in node.names)
    return frozenset(names)


def _defines(slot: Slot) -> frozenset[str] | None:
    """The names ``slot`` defines at module level: its ``name``, or what ``raw`` code binds (``None``: a star import)."""
    if slot.kind != "raw":
        return frozenset({slot.name})
    names: set[str] = set()
    for node in ast.parse(slot.code).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            imported = _binds((ast.unparse(node),))
            if imported is None:
                return None
            names |= imported
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(name.id for target in targets for name in ast.walk(target) if isinstance(name, ast.Name))
    return frozenset(names)


def _item_expression(item: str) -> str:
    """``item`` as an expression, with each of its fields replaced by a placeholder name."""
    return "".join(literal + (FIELD if field is not None else "") for literal, field, _, _ in string.Formatter().parse(item))


def _names(expression: str) -> list[str]:
    """The free names ``expression`` uses, once each, in the order they're written; none when it doesn't parse."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return []
    nodes = list(ast.walk(tree))
    local = {node.arg for node in nodes if isinstance(node, ast.arg)}
    local |= {node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    used = [node for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)]
    used.sort(key=lambda node: (node.lineno, node.col_offset))
    return list(dict.fromkeys(node.id for node in used if node.id not in local))


def _item(text: str) -> str:
    """``text`` if it's a format string whose fields are all names, which ``defaults`` or a contribution can give."""
    try:
        names = [name for _, name, _, _ in string.Formatter().parse(text) if name is not None]
    except ValueError as error:
        raise SpecError(f"{text!r} isn't a format string: {error}.") from None
    for name in names:
        if not name.isidentifier():
            raise SpecError(f"{text!r} uses {{{name}}}, and an item's fields are names, such as {{name}} or {{prefix}}.")
    return text


def _python_name(text: str) -> str:
    if not spec.identifier(text):
        raise SpecError(f"{text!r} isn't a Python name, such as 'ROUTERS'.")
    return text


def _kind_of_slot(text: str) -> str:
    if text not in SLOT_KEYS:
        raise SpecError(f"should be one of {', '.join(sorted(SLOT_KEYS))}, not {text!r}.")
    return text


def _data_format(text: str) -> str:
    if text not in ("ts", "json"):
        raise SpecError(f"should be 'ts' or 'json', not {text!r}.")
    return text


def _param_type(text: str) -> str:
    if text not in PARAM_TYPES:
        raise SpecError(f"should be one of {', '.join(PARAM_TYPES)}, not {text!r}.")
    return text


def param_value(param: Param, text: str) -> str | int:
    """``text``, given for ``param`` on the command line, read by the param's type and checked as its ``default`` is
    (spec 2.9). A ``bool`` param takes no value.

    Raises:
        SpecError: the param's rules refuse it.
    """
    if param.type == "int":
        if re.fullmatch(r"-?[0-9]+", text) is None:
            raise SpecError(f"{text!r} isn't an integer: give an optional '-' and ASCII digits.")
        return int(text)
    if param.type == "choice":
        return _in(param.choices)(text)
    if param.type == "cidr":
        return _network(text, param.private_only, param.max_prefix)
    return text


def _network(text: str, private_only: bool, max_prefix: Prefix | None) -> str:
    """``text`` if it's a network address no wider than ``max_prefix`` allows, and private when ``private_only``."""
    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError:
        raise SpecError(f"{text!r} isn't a network address, such as '172.31.240.0/24'.") from None
    if private_only and not network.is_private:
        raise SpecError(f"{text!r} isn't a private network, and private_only is set.")
    limit = max_prefix.limit(network.version) if max_prefix is not None else None
    if limit is not None and limit > network.max_prefixlen:
        raise SpecError(
            f"{text!r} is an IPv4 network, and max_prefix = {limit} needs an IPv6 one, since IPv4 prefixes stop at /32."
        )
    if limit is not None and network.prefixlen < limit:
        raise SpecError(
            f"{text!r} is wider than max_prefix allows: it's /{network.prefixlen}, and this param takes /{limit} or longer."
        )
    return text


def _template(text: str) -> str:
    """``text`` with ``register``'s fields filled in with sample values, refusing any other field."""
    try:
        fields = [field for _, field, _, _ in string.Formatter().parse(text) if field is not None]
    except ValueError as error:
        raise SpecError(f"{text!r} isn't a format string: {error}.") from None
    for name in fields:
        if name not in TEMPLATE_FIELDS:
            raise SpecError(f"{text!r} uses {{{name}}}, and register templates take {{name}}, {{name_dashed}} and {{Name}}.")
    return text.format(**TEMPLATE_FIELDS)


def _filled(parser: Callable[[str], object]) -> Callable[[str], str]:
    """A rule that checks a template with ``parser`` once its fields are filled in, quoting the template."""

    def rule(text: str) -> str:
        filled = _template(text)
        try:
            parser(filled)
        except SpecError as error:
            raise SpecError(str(error).replace(repr(filled), repr(text), 1)) from None
        return text

    return rule


def _module(text: str) -> str:
    if not all(spec.identifier(part) for part in text.split(".")):
        raise SpecError(f"{text!r} isn't a module name, such as 'scripts.validate_env'.")
    return text


def _outside_project(text: str) -> str:
    if text not in ("error", "token_hex"):
        raise SpecError(f"should be 'error' or 'token_hex', not {text!r}.")
    return text


def _public(text: str) -> str:
    parts = text.removesuffix(".*").split(".")
    if not all(spec.identifier(part) for part in parts):
        raise SpecError(f"{text!r} isn't a module, or a package followed by '.*', such as 'src.infrastructure.composition'.")
    return text


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


def _env_template(text: str) -> str:
    if "\n" in text or "\r" in text:
        raise SpecError(f"{text!r} can't contain a line break (\\n or \\r), since a .env reader would split the line there.")
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


def _first(table: dict[str, Any]) -> object:
    value = next(iter(table.values()), "value")
    return value if not isinstance(value, dict) else "value"


if __name__ == "__main__":
    from fastro.engine.catalog_check import main

    raise SystemExit(main(sys.argv[1:]))
