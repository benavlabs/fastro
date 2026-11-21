"""Names in a loaded ``blueprint.toml`` that refer to another section, checked once every key reads cleanly."""

from __future__ import annotations

import difflib
import string
from collections.abc import Collection, Iterator
from pathlib import PurePath
from typing import TYPE_CHECKING

from fastro.engine import conditions
from fastro.engine.conditions import Condition
from fastro.engine.positions import KeyPath
from fastro.engine.problems import Problem
from fastro.engine.spec import Ref, RepoPath, SpecError

if TYPE_CHECKING:
    from fastro.engine.blueprint import Blueprint, Contribution, Contributions, Slot
    from fastro.engine.project import Project

BUILT_IN_FIELDS = frozenset({"name", "ref", "module", "attr", "value"})
ONE_SLOT_KINDS = ("value", "reexport")
OWN_MODULE = "Point it at the module that defines it."
OWNED_TWICE = "BP003"


def problems(catalog: Blueprint) -> list[Problem]:
    """Every name in ``catalog`` that refers to something it doesn't declare, a ``requires`` cycle, a point, slot
    or contribution that doesn't fit the others (spec 2.3 and 2.6), a path declared twice (1.1), or a feature
    command whose module sits outside its feature's paths (2.9)."""
    return list(_Checker(catalog).run())


def project_problems(project: Project, catalog: Blueprint) -> list[Problem]:
    """Every name in ``project`` that its ``catalog`` doesn't declare, and a pin that doesn't match it (spec 3).

    Against a catalog that extends another, only the pin is checked: its names wait for the merge with its base.
    """
    return list(_ProjectChecker(catalog, project).run())


class _Checker:
    def __init__(self, catalog: Blueprint) -> None:
        self.catalog = catalog
        self.features = {feature.name: feature for feature in catalog.features}
        self.points = catalog.points
        self.slots: dict[str, Slot] = {}
        self.modules: dict[str, str | None] = {}
        for file in catalog.wiring:
            module = _module_of(file.path, catalog.info.python_root)
            for slot in file.slots:
                if slot.kind != "raw" and slot.point in self.points:
                    self.slots.setdefault(slot.point, slot)
                    self.modules.setdefault(slot.point, module)

    def problem(self, path: KeyPath, message: str, fix: str, code: str = "") -> Problem:
        return self.catalog.document.problem(path, message, fix, code)

    def place(self, at: KeyPath) -> str:
        """How a message names the catalog key at ``at``."""
        return self.catalog.document.key(at)

    def run(self) -> Iterator[Problem]:
        catalog = self.catalog
        groups: list[tuple[KeyPath, str, Contributions]] = []
        for feature in catalog.features:
            for index, name in enumerate(feature.requires):
                yield from self.feature((*feature.at, "requires", index), name)
            owner = f"feature {feature.name}"
            groups.append(((*feature.at, "contributes"), owner, feature.contributes))
            for index, when in enumerate(feature.when):
                yield from self.condition((*feature.at, "when", index, "if"), when.condition)
                groups.append(((*feature.at, "when", index, "contributes"), owner, when.contributes))
        yield from self.cycles()
        groups.append((("core", "contributes"), "[core]", catalog.core))
        for name, generator in catalog.generators.items():
            if generator.register is not None:
                at: KeyPath = ("generators", name, "register", "contributes")
                groups.append((at, "", generator.register.parsed))
                for point in generator.register.parsed:
                    yield from self.register_point((*at, point), point)
        for path, _, contributes in groups:
            yield from self.contributions(path, contributes)
        yield from self.owners(groups)
        yield from self.wiring()
        yield from self.paths()
        yield from self.commands()
        for name, members in catalog.presets.items():
            for index, member in enumerate(members):
                yield from self.feature(("presets", name, index), member)
        for name, members in catalog.verify.sets.items():
            for index, member in enumerate(members):
                yield from self.feature(("verify", "sets", name, index), member)
        for check in catalog.verify.checks:
            yield from self.condition((*check.at, "if"), check.condition)
        for file in catalog.wiring:
            yield from self.condition((*file.at, "if"), file.condition)
        if catalog.plugin_api is not None:
            for name in catalog.plugin_api.public_with:
                yield from self.feature((*catalog.plugin_api.at, "public_with", name), name)
            for index, point in enumerate(catalog.plugin_api.points):
                yield from self.plugin_point((*catalog.plugin_api.at, "points", index), point)

    def feature(self, path: KeyPath, name: str) -> Iterator[Problem]:
        if name not in self.features:
            listing = "The features are {}." if self.features else "This catalog declares no features yet."
            fix = _closest(name, self.features, listing)
            yield self.problem(path, f"{name!r} isn't a feature of this catalog.", fix)

    def point(self, path: KeyPath, name: str) -> Iterator[Problem]:
        if name not in self.points:
            listing = "The points are {}." if self.points else "This catalog declares no points yet."
            yield self.problem(path, f"{name!r} isn't a declared point.", _closest(name, self.points, listing))

    def contributions(self, path: KeyPath, contributes: Contributions) -> Iterator[Problem]:
        for point, items in contributes.items():
            yield from self.point((*path, point), point)
            slot = self.slots.get(point)
            for index, item in enumerate(items):
                if slot is not None:
                    yield from self.fits((*path, point, index), item, slot)
                yield from self.condition((*path, point, index, "if"), item.condition)

    def fits(self, path: KeyPath, item: Contribution, slot: Slot) -> Iterator[Problem]:
        forms: tuple[str, ...]
        if slot.literal:
            forms, wanted, kind = ("", "value"), "a string or a value table", "literal"
        elif slot.kind == "data":
            forms, wanted, kind = ("data",), "a data table", "data"
        else:
            forms, wanted, kind = ("", "ref"), "a ref", slot.kind
        if item.via not in forms:
            yield self.problem(path, f"should be {wanted}, since {slot.point} is collected by a {kind} slot.", "")
            return
        ref = item.target if isinstance(item.target, Ref) else None
        if item.via == "" and not slot.literal and isinstance(item.target, str):
            try:
                ref = Ref.parse(item.target)
            except SpecError as error:
                yield self.problem(path, str(error), "")
                return
        module = self.modules.get(slot.point)
        if ref is not None and ref.module == module:
            message = f"{str(ref)!r} is a ref to {module}, the module {self.place(slot.at)} is generated into."
            yield self.problem(path, message, OWN_MODULE)
            return
        given = BUILT_IN_FIELDS | set(slot.defaults) | set(item.fields)
        for name in _fields(slot.item):
            if name not in given:
                message = f"doesn't give {name}, which the item of {self.place(slot.at)} uses."
                yield self.problem(
                    path, message, f"Add {name} to this contribution, or a default for it in that slot's defaults."
                )

    def owners(self, groups: list[tuple[KeyPath, str, Contributions]]) -> Iterator[Problem]:
        line = self.catalog.document.line
        claims = sorted(
            (line((*path, point)), (*path, point), point, owner)
            for path, owner, contributes in groups
            for point in contributes
            if owner and point in self.points and self.points[point].cardinality == "one"
        )
        first: dict[str, str] = {}
        for _, path, point, owner in claims:
            if first.setdefault(point, owner) != owner:
                yield self.problem(path, f"{point!r} is a one point, and {first[point]} contributes to it already.", "")

    def wiring(self) -> Iterator[Problem]:
        seen: dict[str, KeyPath] = {}
        for file in self.catalog.wiring:
            for only in self.catalog.info.all_blueprint_only:
                if only.covers(file.path.text):
                    where = "is" if only.text == file.path.text else f"sits under {only.text!r}, which is"
                    message = f"{file.path.text!r} {where} blueprint_only, and the tool generates this wiring file in projects."
                    yield self.problem((*file.at, "path"), message, "Give the wiring file a path outside blueprint_only.")
                    break
            for slot in file.slots:
                if slot.kind == "raw":
                    continue
                path = (*slot.at, "point")
                if slot.point not in self.points:
                    yield from self.point(path, slot.point)
                elif slot.point in seen:
                    yield self.problem(
                        path, f"{slot.point!r} is collected by {self.catalog.document.key(seen[slot.point])} already.", ""
                    )
                else:
                    seen[slot.point] = slot.at
                    if slot.kind in ONE_SLOT_KINDS and self.points[slot.point].cardinality != "one":
                        message = f"{slot.point!r} is a many point, and a {slot.kind} slot collects a one point."
                        fix = f'Set cardinality = "one" in [points.{slot.point}], or collect it with a tuple or list slot.'
                        yield self.problem(path, message, fix)
        for file in self.catalog.wiring:
            module = _module_of(file.path, self.catalog.info.python_root)
            for slot in file.slots:
                refs = [((*slot.at, "fallback"), slot.fallback)] if isinstance(slot.fallback, Ref) else []
                refs += [((*slot.at, "append", index), ref) for index, ref in enumerate(slot.append)]
                for at, ref in refs:
                    if ref.module == module:
                        message = f"{str(ref)!r} is a ref to {module}, the module this slot is generated into."
                        yield self.problem(at, message, OWN_MODULE)
        for name, point in self.points.items():
            if name not in seen:
                fix = f"Add a slot with point = {name!r} to a [[wiring]] file, or drop the point."
                yield self.problem(point.at, "isn't collected by any slot.", fix)

    def register_point(self, path: KeyPath, name: str) -> Iterator[Problem]:
        if name in self.points and self.points[name].cardinality != "many":
            message = f"{name!r} is a one point, and register contributes only to many points."
            fix = f'Set cardinality = "many" in [points.{name}], or leave it out of register.contributes.'
            yield self.problem(path, message, fix)

    def declarations(self) -> list[tuple[KeyPath, RepoPath]]:
        """Every path a feature or one of its ``when`` tables declares, with where it's declared."""
        return [
            (at, path)
            for feature in self.catalog.features
            for at, path in [
                *(((*feature.at, "paths", index), path) for index, path in enumerate(feature.paths)),
                *(
                    ((*feature.at, "when", number, "paths", index), path)
                    for number, when in enumerate(feature.when)
                    for index, path in enumerate(when.paths)
                ),
            ]
        ]

    def paths(self) -> Iterator[Problem]:
        line = self.catalog.document.line
        first: dict[str, KeyPath] = {}
        for _, at, text in sorted((line(at), at, path.text) for at, path in self.declarations()):
            if text in first:
                fix = "A path belongs to one declaration: drop one of them."
                yield self.problem(
                    at, f"{text!r} is declared by {self.catalog.document.key(first[text])} already.", fix, OWNED_TWICE
                )
            else:
                first[text] = at

    def commands(self) -> Iterator[Problem]:
        root = self.catalog.info.python_root
        declarations = self.declarations()
        for feature in self.catalog.features:
            own = {(*feature.at, "paths", index) for index in range(len(feature.paths))}
            for command in feature.commands.values():
                stem = f"{(command.cwd or root).text.removesuffix('/')}/{command.module.replace('.', '/')}"
                owners = {target: _owner(declarations, target) for target in (f"{stem}.py", f"{stem}/")}
                path = (*command.at, "module")
                instead = ", or declare the command in [commands] instead."
                taken = [(target, at) for target, at in owners.items() if at is not None and at not in own]
                if taken:
                    target, at = taken[0]
                    message = (
                        f"{command.module!r} is {target}, which is owned by {self.catalog.document.key(at)}, "
                        "and a feature command's module must be owned by its feature's own paths."
                    )
                    if at[: len(feature.at)] == feature.at:
                        fix = f"Move it out of {self.catalog.document.key(at)}"
                    else:
                        fix = f"Give [features.{feature.name}] a path that owns it, longer than {self.catalog.document.key(at)}"
                    yield self.problem(path, message, fix + instead)
                elif all(at is None for at in owners.values()):
                    file, package = owners
                    message = (
                        f"{command.module!r} is {file}, or {package} as a package, "
                        f"and neither is under the paths of feature {feature.name}."
                    )
                    yield self.problem(path, message, f"Add it to the paths of [features.{feature.name}]" + instead)

    def plugin_point(self, path: KeyPath, name: str) -> Iterator[Problem]:
        if name not in self.points:
            yield from self.point(path, name)
        elif self.points[name].cardinality != "many":
            message = f"{name!r} is a one point, and plugins contribute only to many points."
            fix = f'Set cardinality = "many" in [points.{name}], or leave it out of plugin_api.points.'
            yield self.problem(path, message, fix)

    def condition(self, path: KeyPath, condition: Condition | None) -> Iterator[Problem]:
        if condition is None:
            return
        unknown: set[str] = set()
        for atom in dict.fromkeys(conditions.atoms(condition)):
            name = atom.feature if isinstance(atom, conditions.Option) else atom.name
            if name not in self.features and name not in unknown:
                unknown.add(name)
                yield from self.feature(path, name)
            elif isinstance(atom, conditions.Option) and name in self.features:
                yield from self.option(path, atom)

    def option(self, path: KeyPath, atom: conditions.Option) -> Iterator[Problem]:
        options = self.features[atom.feature].options
        if atom.option not in options:
            listing = f"The options of {atom.feature} are {{}}." if options else f"{atom.feature} has no options."
            fix = _closest(atom.option, options, listing)
            yield self.problem(path, f"'{atom.feature}.{atom.option}' isn't an option of {atom.feature}.", fix)
        elif atom.value not in options[atom.option].choices:
            choices = ", ".join(options[atom.option].choices)
            message = f"{atom.value!r} isn't a choice of {atom.feature}.{atom.option}."
            yield self.problem(path, message, f"The choices are {choices}.")

    def cycles(self) -> Iterator[Problem]:
        done: set[str] = set()
        for start in self.features:
            if start in done:
                continue
            trail: list[str] = []
            stack = [(start, 0)]
            while stack:
                name, index = stack.pop()
                if index == 0:
                    trail.append(name)
                requires = self.features[name].requires
                while index < len(requires) and (requires[index] in done or requires[index] not in self.features):
                    index += 1
                if index == len(requires):
                    done.add(name)
                    trail.pop()
                    continue
                stack.append((name, index + 1))
                target = requires[index]
                if target in trail:
                    loop = " → ".join([*trail[trail.index(target) :], target])
                    path = (*self.features[name].at, "requires", index)
                    yield self.problem(path, f"{target!r} closes a cycle: {loop}.", "Drop one of the requires in that cycle.")
                else:
                    stack.append((target, 0))


def _module_of(path: RepoPath, python_root: RepoPath) -> str | None:
    """The dotted module a generated Python file under ``python_root`` is, or ``None`` for a data file (2.8)."""
    root = python_root.text.removesuffix("/") + "/"
    if not path.text.startswith(root) or not path.text.endswith(".py"):
        return None
    parts = path.text[len(root) : -len(".py")].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _fields(item: str) -> list[str]:
    """The fields an ``item`` format string uses, in order, each once."""
    return list(dict.fromkeys(field for _, field, _, _ in string.Formatter().parse(item) if field))


def _owner(declarations: list[tuple[KeyPath, RepoPath]], target: str) -> KeyPath | None:
    """Where the declaration that owns ``target`` is: the one with the longest path that takes it in (spec 1.1)."""
    matches = [(len(path.text), at) for at, path in declarations if path.covers(target)]
    return max(matches, key=lambda match: match[0])[1] if matches else None


def _closest(name: str, names: Collection[str], listing: str) -> str:
    """A fix suggesting the name closest to ``name``, or else ``listing`` filled with every name."""
    close = difflib.get_close_matches(name, names, n=1)
    return f"Did you mean {close[0]!r}?" if close else listing.format(", ".join(names))


class _ProjectChecker(_Checker):
    def __init__(self, catalog: Blueprint, project: Project) -> None:
        super().__init__(catalog)
        self.project = project

    def problem(self, path: KeyPath, message: str, fix: str, code: str = "") -> Problem:
        return self.project.document.problem(path, message, fix, code)

    def place(self, at: KeyPath) -> str:
        return f"{PurePath(self.catalog.document.file).name}'s {self.catalog.document.key(at)}"

    def run(self) -> Iterator[Problem]:
        pin, info, project = self.project.blueprint, self.catalog.info, self.project
        if pin.name != info.name:
            fix = f"Set blueprint.name in fastro.toml to {info.name!r}, the catalog's name."
            yield self.problem(("blueprint", "name"), f"is {pin.name!r}, and the catalog is {info.name!r}.", fix)
        if pin.version != info.version:
            fix = f"Set blueprint.version in fastro.toml to {info.version}, the catalog's version."
            yield self.problem(("blueprint", "version"), f"is {pin.version}, and the catalog is {info.version}.", fix)
        if info.extends is not None:
            return
        for index, name in enumerate(project.features):
            yield from self.feature(("features", index), name)
        for index, name in enumerate(project.app.requires):
            yield from self.feature(("app", "requires", index), name)
        for (feature, option), value in project.options.items():
            path = ("options", f"{feature}.{option}")
            if feature in self.features:
                yield from self.option(path, conditions.Option(feature, option, value))
            else:
                yield from self.feature(path, feature)
        yield from self.contributions(("app", "contributes"), project.app.contributes)
        yield from self.app_paths()

    def app_paths(self) -> Iterator[Problem]:
        first = {path.text: self.place(at) for at, path in self.declarations()}
        for index, path in enumerate(self.project.app.paths):
            at = ("app", "paths", index)
            covered = next((file.path.text for file in self.catalog.wiring if path.covers(file.path.text)), None)
            if covered is not None:
                what = "is" if covered == path.text else f"covers {covered!r},"
                message = f"{path.text!r} {what} a wiring file the tool generates."
                yield self.problem(at, message, "Narrow [app].paths so they leave the wiring files out.")
            if path.text in first:
                fix = "A path belongs to one declaration: drop one of them."
                yield self.problem(at, f"{path.text!r} is declared by {first[path.text]} already.", fix, OWNED_TWICE)
            else:
                first[path.text] = self.project.document.key(at)
