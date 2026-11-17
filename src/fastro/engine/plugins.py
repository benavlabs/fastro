"""Loading a plugin's ``fastro-plugin.toml`` (spec 5), key by key.

Only what the manifest alone can show is checked here. What needs the blueprint it's added to waits for
``fastro add`` and ``fastro plugin lint``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from fastro.engine import spec
from fastro.engine.blueprint import FEATURE_KEYS, Command, Feature, Generator, Reader
from fastro.engine.positions import KeyPath
from fastro.engine.problems import CatalogError, Document, read
from fastro.engine.spec import RepoPath, SpecError

TOP_LEVEL = ("plugin", "feature", "commands", "generators")
PLUGIN_KEYS = ("name", "version", "summary", "license", "homepage", "group", "targets")
MANIFEST_FEATURE_KEYS = (FEATURE_KEYS - {"commands"}) | {"env"}
TARGETS_FIX = "Add a [plugin.targets.fastapi-boilerplate] table with blueprint and api."


@dataclass(frozen=True)
class Target:
    """A blueprint the plugin works with: which of its versions, and the plugin API it was written for."""

    blueprint: SpecifierSet
    api: int


@dataclass(frozen=True)
class PluginInfo:
    """The ``[plugin]`` table (spec 5)."""

    name: str
    version: Version
    summary: str
    license: str = ""
    homepage: str = ""
    group: str = ""
    targets: Mapping[str, Target] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    """A loaded ``fastro-plugin.toml``, with the document it came from for placing later problems."""

    plugin: PluginInfo
    feature: Feature
    env: RepoPath | None = None
    commands: Mapping[tuple[str, ...], Command] = field(default_factory=dict)
    generators: Mapping[str, Generator] = field(default_factory=dict)
    document: Document = field(compare=False, repr=False, kw_only=True)


def load(file: str, text: str) -> Manifest:
    """``text`` as the ``fastro-plugin.toml`` named ``file``, or a :class:`CatalogError` naming every problem in it."""
    document = read(file, text)
    reader = _ManifestReader(document)
    manifest = reader.manifest(document.data)
    if reader.problems or manifest is None:
        raise CatalogError(reader.problems)
    return manifest


class _ManifestReader(Reader):
    top = "fastro-plugin.toml"

    def manifest(self, data: dict[str, Any]) -> Manifest | None:
        self.table((), data, TOP_LEVEL)
        if "plugin" not in data:
            self.fail(("plugin",), "is required.", "Add a [plugin] table with name, version, summary and a target.")
        if "feature" not in data:
            fix = "Add a [feature] table with summary, as for a blueprint's [features.<name>]."
            self.fail(("feature",), "is required.", fix)
        plugin = self.plugin(("plugin",), data["plugin"]) if "plugin" in data else None
        feature, env = self.feature_of(("feature",), plugin, data.get("feature"))
        commands = [self.command(at, words, item) for at, words, item in self.named((), data, "commands", spec.command_words)]
        generators = [
            self.generator(at, name, item) for at, name, item in self.named((), data, "generators", spec.generator_name)
        ]
        if plugin is None or feature is None:
            return None
        return Manifest(
            plugin=plugin,
            feature=feature,
            env=env,
            commands={command.words: command for command in commands if command is not None},
            generators={generator.name: generator for generator in generators if generator is not None},
            document=self.document,
        )

    def plugin(self, path: KeyPath, value: object) -> PluginInfo | None:
        table = self.table(path, value, PLUGIN_KEYS, ("name", "version", "summary"))
        if table is None:
            return None
        name = self.text(path, table, "name", spec.feature_name)
        version = self.text(path, table, "version", spec.version)
        summary = self.text(path, table, "summary", str)
        license_, homepage, group = (self.text(path, table, key, str) for key in ("license", "homepage", "group"))
        targets = self.targets((*path, "targets"), table)
        if name is None or version is None or summary is None:
            return None
        return PluginInfo(name, version, summary, license_ or "", homepage or "", group or "", targets)

    def targets(self, path: KeyPath, table: dict[str, Any]) -> dict[str, Target]:
        if "targets" not in table:
            self.fail(path, "is required.", TARGETS_FIX)
            return {}
        if table["targets"] == {}:
            self.fail(path, "should list at least one target.", TARGETS_FIX)
        targets: dict[str, Target] = {}
        for at, name, value in self.named(path[:-1], table, "targets", spec.blueprint_name):
            target = self.table(at, value, ("blueprint", "api"), ("blueprint", "api"))
            if target is None:
                continue
            blueprint = self.text(at, target, "blueprint", spec.specifier)
            api = self.number(at, target, "api")
            if api is not None and api < 1:
                self.fail((*at, "api"), f"should be 1 or more, not {api}.")
            if blueprint is not None and api is not None and api >= 1:
                targets[name] = Target(blueprint, api)
        return targets

    def feature_of(self, path: KeyPath, plugin: PluginInfo | None, value: object) -> tuple[Feature | None, RepoPath | None]:
        if not isinstance(value, dict):
            if value is not None:
                self.wrong(path, "a table", value)
            return None, None
        if "commands" in value:
            message = "isn't allowed in a manifest: a plugin's commands go in its top-level [commands] table."
            self.fail((*path, "commands"), message)
        self.table(path, {key: item for key, item in value.items() if key != "commands"}, MANIFEST_FEATURE_KEYS)
        env = self.text(path, value, "env", _env)
        shared = {key: item for key, item in value.items() if key in MANIFEST_FEATURE_KEYS - {"env"}}
        feature = self.feature(path, plugin.name if plugin is not None else "", shared)
        return feature, env


def _env(text: str) -> RepoPath:
    path = spec.repo_path(text, root="the directory holding fastro-plugin.toml")
    if path.subtree:
        raise SpecError(f"{text!r} names a directory, and env names a file.")
    return path
