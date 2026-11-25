"""A project's ``.fastro/lock.toml`` (spec 4): loading it key by key, and writing it."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from packaging.version import Version

from fastro.engine import spec
from fastro.engine.blueprint import UPGRADE, CrossReferenceError, IncompatibleCatalog, Reader
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Document, Problem, read
from fastro.engine.project import toml_string
from fastro.engine.spec import RepoPath, SpecError

if TYPE_CHECKING:
    from fastro.engine.project import Project

TOP_LEVEL = ("lock", "plugins", "files")
LOCK_KEYS = ("schema", "fastro", "blueprint", "features", "options", "starter")
PROVENANCE_KEYS = ("name", "version", "source", "commit")
SCHEMA = 1
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ORIGINS = ("core", "wiring", "overlay", "deps", "env")
NAMED_ORIGINS = {"feature": spec.feature_name, "plugin": spec.feature_name, "make": spec.generator_name}
GIT_SOURCES = ("gh:", "git+")
LOCK_FILE = ".fastro/lock.toml"
PATH_SOURCES = ("/", ".")


@dataclass(frozen=True)
class Provenance:
    """Where the blueprint or the starter came from, down to the commit when it's a git source (spec 4)."""

    name: str
    version: Version
    source: str
    commit: str | None = None


@dataclass(frozen=True)
class LockedPlugin:
    """A plugin at the version and commit the project was built with (spec 4)."""

    name: str
    source: str
    version: Version
    commit: str | None = None


@dataclass(frozen=True)
class LockedFile:
    """A file the tool wrote: its hash, which declaration it came from, and whether it's linked (spec 4)."""

    sha256: str
    origin: str
    linked: bool = False


@dataclass(frozen=True)
class Lock:
    """A ``lock.toml``, loaded with the document it came from for placing later problems, or made in memory."""

    schema: int
    fastro: Version
    blueprint: Provenance
    features: tuple[str, ...]
    options: Mapping[tuple[str, str], str] = field(default_factory=dict)
    starter: Provenance | None = None
    plugins: Mapping[str, LockedPlugin] = field(default_factory=dict)
    files: Mapping[RepoPath, LockedFile] = field(default_factory=dict)
    document: Document = field(default_factory=lambda: read(LOCK_FILE, ""), compare=False, repr=False, kw_only=True)


def load(file: str, text: str, project: Project | None = None) -> Lock:
    """``text`` as the ``lock.toml`` named ``file``, or a :class:`CatalogError` naming every problem in it.

    A lock whose ``schema`` is newer than this fastro reads is refused with only that problem. With ``project``, its
    ``fastro.toml``, the pin and the plugins are checked against it once every key reads cleanly (spec 4).
    """
    document = read(file, text)
    reader = _LockReader(document)
    table = document.data.get("lock")
    if isinstance(table, dict) and isinstance(table.get("schema"), int) and table["schema"] > SCHEMA:
        reader.fail(("lock", "schema"), f"is {table['schema']}, which this fastro doesn't read.", UPGRADE)
        raise IncompatibleCatalog(reader.problems)
    lock = reader.lock(document.data)
    if reader.problems or lock is None:
        raise CatalogError(reader.problems)
    if project is not None and (found := _against(lock, project)):
        raise CrossReferenceError(found)
    return lock


def dump(lock: Lock) -> str:
    """``lock`` as ``lock.toml`` text, laid out as spec 4 shows it: ``[lock]``, then each ``[plugins.<name>]``, then
    ``[files]`` sorted by path, leaving out each optional key that's empty. Features, options and plugins are written
    in the order ``lock`` gives them."""
    lines = [
        "[lock]",
        f"schema = {lock.schema}",
        f"fastro = {toml_string(str(lock.fastro))}",
        f"blueprint = {_provenance(lock.blueprint)}",
        f"features = [{', '.join(map(toml_string, lock.features))}]",
    ]
    if lock.options:
        pairs = (
            f"{toml_string(f'{feature}.{option}')} = {toml_string(value)}" for (feature, option), value in lock.options.items()
        )
        lines.append(f"options = {{ {', '.join(pairs)} }}")
    if lock.starter is not None:
        lines += ["", f"starter = {_provenance(lock.starter)}"]
    for name, plugin in lock.plugins.items():
        lines += [
            "",
            f"[plugins.{name}]",
            f"source = {toml_string(plugin.source)}",
            f"version = {toml_string(str(plugin.version))}",
        ]
        if plugin.commit is not None:
            lines.append(f"commit = {toml_string(plugin.commit)}")
    if lock.files:
        lines += ["", "[files]"]
        for path in sorted(lock.files, key=lambda path: path.text):
            entry = lock.files[path]
            linked = ", linked = true" if entry.linked else ""
            sha256, origin = toml_string(entry.sha256), toml_string(entry.origin)
            lines.append(f"{toml_string(path.text)} = {{ sha256 = {sha256}, origin = {origin}{linked} }}")
    return "\n".join(lines) + "\n"


def _provenance(provenance: Provenance) -> str:
    pairs = [("name", provenance.name), ("version", str(provenance.version)), ("source", provenance.source)]
    if provenance.commit is not None:
        pairs.append(("commit", provenance.commit))
    return f"{{ {', '.join(f'{key} = {toml_string(value)}' for key, value in pairs)} }}"


def _against(lock: Lock, project: Project) -> list[Problem]:
    """Where the lock and ``fastro.toml`` disagree: the pin, or a plugin one of them lists and the other doesn't."""
    found: list[Problem] = []
    pin, locked = project.blueprint, lock.blueprint
    catalog = "blueprint.toml" if project.source else ".fastro/blueprint.toml"
    fix = f"fastro writes both together, so one was edited by hand. Make them agree with {catalog}."
    if locked.name != pin.name:
        message = f"is {locked.name!r}, and fastro.toml pins {pin.name!r}."
        found.append(lock.document.problem(("lock", "blueprint", "name"), message, fix))
    if locked.version != pin.version:
        message = f"is {locked.version}, and fastro.toml pins {pin.version}."
        found.append(lock.document.problem(("lock", "blueprint", "version"), message, fix))
    for name in lock.plugins.keys() - project.plugins.keys():
        message = "is locked, but fastro.toml's [plugins] doesn't list it."
        fix = f"fastro writes both together: add {name} back to fastro.toml's [plugins], or drop [plugins.{name}] here."
        found.append(lock.document.problem(("plugins", name), message, fix))
    for name in project.plugins.keys() - lock.plugins.keys():
        message = f"is in fastro.toml's [plugins], but the lock has no [plugins.{name}]."
        fix = f"fastro writes both together: drop {name} from [plugins], or lock the version and commit it was built with."
        found.append(project.document.problem(("plugins", name), message, fix))
    return found


class _LockReader(Reader):
    top = "lock.toml"

    def lock(self, data: dict[str, Any]) -> Lock | None:
        self.table((), data, TOP_LEVEL)
        if "lock" not in data:
            self.fail(("lock",), "is required.", "Add a [lock] table: fastro writes it when it makes or changes a project.")
            return None
        path: KeyPath = ("lock",)
        table = self.table(path, data["lock"], LOCK_KEYS, ("schema", "fastro", "blueprint", "features"))
        if table is None:
            return None
        schema = self.number(path, table, "schema")
        if schema is not None and schema < SCHEMA:
            self.fail((*path, "schema"), f"should be {SCHEMA}, not {schema}.")
        fastro = self.text(path, table, "fastro", spec.version)
        blueprint = (
            self.provenance((*path, "blueprint"), table["blueprint"], spec.blueprint_name) if "blueprint" in table else None
        )
        features = self.texts(path, table, "features", spec.feature_name)
        self.repeats((*path, "features"), table.get("features"))
        options = self.options((*path, "options"), self.subtable(path, table, "options"))
        starter = self.provenance((*path, "starter"), table["starter"], spec.starter_name) if "starter" in table else None
        plugins = [self.plugin(at, name, value) for at, name, value in self.named((), data, "plugins", spec.feature_name)]
        files: dict[RepoPath, LockedFile] = {}
        for at, name, value in self.named((), data, "files", _file):
            entry = self.file(at, value)
            if entry is not None:
                files[RepoPath(name)] = entry
        if schema is None or fastro is None or blueprint is None:
            return None
        return Lock(
            schema=schema,
            fastro=fastro,
            blueprint=blueprint,
            features=features,
            options=options,
            starter=starter,
            plugins={plugin.name: plugin for plugin in plugins if plugin is not None},
            files=files,
            document=self.document,
        )

    def provenance(self, path: KeyPath, value: object, rule: Callable[[str], str]) -> Provenance | None:
        table = self.table(path, value, PROVENANCE_KEYS, ("name", "version", "source"))
        if table is None:
            return None
        name = self.text(path, table, "name", rule)
        version = self.text(path, table, "version", spec.version)
        source = self.text(path, table, "source", spec.source)
        commit = self.commit(path, table, source)
        if name is None or version is None or source is None:
            return None
        return Provenance(name, version, source, commit)

    def plugin(self, path: KeyPath, name: str, value: object) -> LockedPlugin | None:
        table = self.table(path, value, ("source", "version", "commit"), ("source", "version"))
        if table is None:
            return None
        source = self.text(path, table, "source", spec.source)
        version = self.text(path, table, "version", spec.version)
        commit = self.commit(path, table, source)
        if source is None or version is None:
            return None
        return LockedPlugin(name, source, version, commit)

    def commit(self, path: KeyPath, table: dict[str, Any], source: str | None) -> str | None:
        commit = self.text(path, table, "commit", _commit)
        if source is None:
            return commit
        if source.startswith(GIT_SOURCES) and "commit" not in table:
            self.fail(
                (*path, "commit"),
                "is required for a gh: or git+ source.",
                "Add commit, the full git commit that source resolved to.",
            )
        elif not source.startswith(GIT_SOURCES + PATH_SOURCES) and "commit" in table:
            self.fail((*path, "commit"), "is only for a gh:, git+ or path source.")
        return commit

    def file(self, path: KeyPath, value: object) -> LockedFile | None:
        table = self.table(path, value, ("sha256", "origin", "linked"), ("sha256", "origin"))
        if table is None:
            return None
        sha256 = self.text(path, table, "sha256", _sha256)
        origin = self.text(path, table, "origin", _origin)
        linked = self.flag(path, table, "linked")
        if "linked" in table and origin is not None and not origin.startswith("plugin:"):
            self.fail((*path, "linked"), "is only for a plugin:<name> origin.")
        if sha256 is None or origin is None:
            return None
        return LockedFile(sha256, origin, linked is True)


def _commit(text: str) -> str:
    if not COMMIT.fullmatch(text):
        raise SpecError(f"{text!r} isn't a full git commit: write its 40 or 64 lowercase hex characters.")
    return text


def _sha256(text: str) -> str:
    if not SHA256.fullmatch(text):
        raise SpecError(f"{text!r} isn't a sha256: write its 64 lowercase hex characters.")
    return text


def _origin(text: str) -> str:
    """``text``, if it's a fixed origin or ``feature:``, ``plugin:`` or ``make:`` and a name that follows its rule."""
    kind, colon, name = text.partition(":")
    if (not colon and text in ORIGINS) or (colon and kind in NAMED_ORIGINS and _named(NAMED_ORIGINS[kind], name, text)):
        return text
    raise SpecError(
        f"{text!r} isn't an origin: use core, wiring, overlay, deps, env, feature:<name>, plugin:<name> or make:<generator>."
    )


def _named(rule: Callable[[str], str], name: str, text: str) -> bool:
    try:
        rule(name)
    except SpecError as error:
        raise SpecError(f"{text!r} isn't an origin: {error}") from None
    return True


def _file(text: str) -> str:
    if spec.repo_path(text).subtree:
        raise SpecError(f"{text!r} names a directory, and [files] lists files.")
    return text
