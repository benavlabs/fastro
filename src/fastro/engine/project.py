"""Loading a project's ``fastro.toml`` (spec 3), key by key."""

from __future__ import annotations

import difflib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine import references, spec
from fastro.engine.blueprint import Blueprint, Contributions, CrossReferenceError, Reader
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Document, read
from fastro.engine.spec import RepoPath, SpecError

TOP_LEVEL = ("blueprint", "source", "features", "project", "options", "app", "plugins", "checks")
REQUIRED = {
    "blueprint": 'Add blueprint = { name = "fastro", version = "0.21.0" }, naming the catalog this project was made from.',
    "features": "Add features = [] for core only, or list the features this project uses.",
    "project": 'Add a [project] table with name, such as name = "my-api".',
}
PROJECT_NAME = re.compile(r"[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]", re.IGNORECASE)


@dataclass(frozen=True)
class Pin:
    """The catalog a project was made from, at the version it was made from (spec 3)."""

    name: str
    version: Version


@dataclass(frozen=True)
class App:
    """The project's own code, as the pseudo-feature ``app`` (spec 3)."""

    requires: tuple[str, ...] = ()
    paths: tuple[RepoPath, ...] = ()
    contributes: Contributions = field(default_factory=dict)


@dataclass(frozen=True)
class Plugin:
    """A plugin the project uses: where it comes from, and which of its versions it takes (spec 3)."""

    name: str
    source: str
    version: SpecifierSet | None
    at: KeyPath = ()


@dataclass(frozen=True)
class Project:
    """A loaded ``fastro.toml``, with the document it came from for placing later problems."""

    blueprint: Pin
    name: str
    features: tuple[str, ...]
    source: bool = False
    options: Mapping[tuple[str, str], str] = field(default_factory=dict)
    app: App = field(default_factory=App)
    plugins: Mapping[str, Plugin] = field(default_factory=dict)
    skip: tuple[str, ...] = ()
    strict: bool = False
    document: Document = field(compare=False, repr=False, kw_only=True)

    @property
    def slug(self) -> str:
        """The name normalized as PEP 503 does: lowercase, with each run of ``-``, ``_`` and ``.`` as one ``-``."""
        return re.sub(r"[-_.]+", "-", self.name).lower()


def load(file: str, text: str, catalog: Blueprint | None = None) -> Project:
    """``text`` as the ``fastro.toml`` named ``file``, or a :class:`CatalogError` naming every problem in it.

    With ``catalog``, the names in it are checked against that catalog once every key reads cleanly (spec 3).
    """
    document = read(file, text)
    reader = _ProjectReader(document)
    project = reader.project(document.data)
    if reader.problems or project is None:
        raise CatalogError(reader.problems)
    if catalog is not None and (found := references.project_problems(project, catalog)):
        raise CrossReferenceError(found)
    return project


class _ProjectReader(Reader):
    def unknown(self, path: KeyPath, key: str, keys: Collection[str], where: str = "") -> None:
        if path:
            super().unknown(path, key, keys, where)
            return
        close = difflib.get_close_matches(key, keys, n=1)
        fix = f"Did you mean '{close[0]}'?" if close else f"The top-level keys are {', '.join(sorted(keys))}."
        self.fail((key,), "isn't a top-level key of fastro.toml.", fix)

    def project(self, data: dict[str, Any]) -> Project | None:
        self.table((), data, TOP_LEVEL)
        for key, fix in REQUIRED.items():
            if key not in data:
                self.fail((key,), "is required.", fix)
        pin = self.pin(("blueprint",), data["blueprint"]) if "blueprint" in data else None
        source = self.flag((), data, "source")
        features = self.texts((), data, "features", spec.feature_name)
        self.repeats(("features",), data.get("features"))
        table = self.table(("project",), data["project"], ("name",), ("name",)) if "project" in data else None
        name = self.text(("project",), table, "name", _project_name) if table is not None else None
        options = self.options(("options",), self.subtable((), data, "options"))
        app = self.app(("app",), self.subtable((), data, "app", ("requires", "paths", "contributes")))
        plugins = [self.plugin(at, plugin, value) for at, plugin, value in self.named((), data, "plugins", spec.feature_name)]
        checks = self.subtable((), data, "checks", ("skip", "strict"))
        if pin is None or name is None:
            return None
        return Project(
            blueprint=pin,
            name=name,
            features=features,
            source=source is True,
            options=options,
            app=app,
            plugins={plugin.name: plugin for plugin in plugins if plugin is not None},
            skip=self.texts(("checks",), checks, "skip", spec.check_code),
            strict=self.flag(("checks",), checks, "strict") is True,
            document=self.document,
        )

    def pin(self, path: KeyPath, value: object) -> Pin | None:
        table = self.table(path, value, ("name", "version"), ("name", "version"))
        if table is None:
            return None
        name = self.text(path, table, "name", spec.blueprint_name)
        version = self.text(path, table, "version", spec.version)
        return Pin(name, version) if name is not None and version is not None else None

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
            pair = self.parse((*path, key), key, _option_key)
            chosen = self.string((*path, key), value, str)
            if pair is not None and chosen is not None:
                options[pair] = chosen
        return options

    def app(self, path: KeyPath, table: dict[str, Any]) -> App:
        return App(
            requires=self.texts(path, table, "requires", spec.feature_name),
            paths=self.texts(path, table, "paths", spec.repo_path),
            contributes=self.contributes(path, table),
        )

    def plugin(self, path: KeyPath, name: str, value: object) -> Plugin | None:
        if isinstance(value, str):
            where = self.string(path, value, spec.source)
            return Plugin(name, where, None, path) if where is not None else None
        if not isinstance(value, dict):
            self.wrong(path, "a source string or a table", value)
            return None
        table = self.table(path, value, ("source", "version"), ("source",)) or {}
        where = self.text(path, table, "source", spec.source)
        version = self.text(path, table, "version", spec.specifier)
        return Plugin(name, where, version, path) if where is not None else None


def _project_name(text: str) -> str:
    if not PROJECT_NAME.fullmatch(text):
        raise SpecError(
            f"{text!r} isn't a project name: use letters, digits, '.', '_' and '-', "
            "starting and ending with a letter or digit, as PEP 508 says."
        )
    return text


def _option_key(text: str) -> tuple[str, str]:
    feature, dot, option = text.partition(".")
    if not dot:
        raise SpecError(f"{text!r} isn't a feature.option key, such as 'cache.backend'.")
    return spec.feature_name(feature), spec.option_name(option)


def _first(table: dict[str, Any]) -> object:
    value = next(iter(table.values()), "value")
    return value if not isinstance(value, dict) else "value"
