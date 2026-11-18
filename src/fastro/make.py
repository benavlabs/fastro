"""``fastro make``: render one of a blueprint's generators into the repo (spec 2.9)."""

from __future__ import annotations

import codecs
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from fastro.apply import breaks_the_rule, file_kind
from fastro.engine import conditions
from fastro.engine.blueprint import MODULE_GENERATOR, Blueprint, Generator, Param, param_value
from fastro.engine.resolve import Selection
from fastro.engine.spec import OPERATORS, SpecError
from fastro.sync import Source

OUTPUT = ("-o", "--output-dir")
FORCE = ("-f", "--force")
YES = ("-y", "--yes")
DRY_RUN = "--dry-run"
HELP = "--help"
OWN_FLAGS = ("--output-dir", "--force", "--yes", DRY_RUN, HELP)
TEMPLATE = ".j2"


class UsageError(Exception):
    """``fastro make`` was given arguments it doesn't take (spec 8: exit 2)."""


class Refused(Exception):
    """The run would break a rule, a template doesn't render, or a generator's flags clash (spec 8: exit 1)."""


@dataclass(frozen=True)
class Request:
    """What ``fastro make`` was asked to do: the generator, its mode, the values given and make's own flags."""

    generator: Generator
    mode: str | None
    values: Mapping[str, str | int | bool]
    output: str | None = None
    force: bool = False
    dry_run: bool = False
    help: bool = False


@dataclass(frozen=True)
class File:
    """A file the run writes: its path under the output directory, the template it comes from, and its bytes."""

    path: str
    template: str
    data: bytes = field(repr=False)


def wants_help(args: Sequence[str]) -> bool:
    """Whether ``args`` asks for help: nothing at all, or ``--help`` anywhere."""
    return not args or HELP in args


def flags(generator: Generator) -> dict[str, tuple[Param, bool | None]]:
    """Each flag ``generator``'s params take, with its param, and for a ``bool`` param the value the flag sets.

    Raises:
        Refused: a param's flag, or a ``bool`` param's ``--no-`` flag, is one of make's own or another param's.
    """
    found: dict[str, tuple[Param, bool | None]] = {}
    for param in generator.params.values():
        flag = f"--{param.name.replace('_', '-')}"
        taken = [(flag, True), (f"--no-{flag[2:]}", False)] if param.type == "bool" else [(flag, None)]
        for name, value in taken:
            if name in OWN_FLAGS or name in found:
                owner = "fastro make's own" if name in OWN_FLAGS else f"param {found[name][0].name}'s"
                raise Refused(
                    f"generators.{generator.name}.params.{param.name} takes the flag {name}, which is {owner}. "
                    "Rename the param in blueprint.toml."
                )
            found[name] = (param, value)
    return found


def parse(catalog: Blueprint, args: Sequence[str]) -> Request:
    """``args``, the words after ``fastro make``, as a request.

    Raises:
        UsageError: an unknown generator or mode, a missing mode, a flag it doesn't take or a value it refuses.
        Refused: the generator's flags clash.
    """
    name = args[0]
    generator = catalog.generators.get(name)
    if name == MODULE_GENERATOR:
        raise UsageError("fastro make module comes with make module's own arguments, which aren't here yet.")
    if generator is None:
        known = ", ".join(sorted(set(catalog.generators) - {MODULE_GENERATOR})) or "none"
        raise UsageError(f"{name!r} isn't a generator this blueprint declares. Its generators: {known}.")
    taken = flags(generator)
    rest = list(args[1:])
    mode = None
    if generator.modes and rest and not rest[0].startswith("-"):
        mode = rest.pop(0)
        if mode not in generator.modes:
            raise UsageError(f"{mode!r} isn't a mode of {name}. Its modes: {', '.join(generator.modes)}.")
    elif generator.modes and HELP not in rest:
        raise UsageError(f"fastro make {name} takes a mode first: {', '.join(generator.modes)}.")
    request = _flags(generator, mode, rest, taken)
    if generator.modes and mode is None and not request.help:
        raise UsageError(f"fastro make {name} takes a mode first: {', '.join(generator.modes)}.")
    return request


def _flags(generator: Generator, mode: str | None, rest: list[str], taken: Mapping[str, tuple[Param, bool | None]]) -> Request:
    given: dict[str, str | int | bool] = {}
    seen: set[str] = set()
    own: dict[str, str | bool] = {}
    while rest:
        token = rest.pop(0)
        flag, equals, inline = token.partition("=") if token.startswith("--") else (token, "", "")
        flag = {short: long for short, long in (OUTPUT, FORCE, YES)}.get(flag, flag)
        if not flag.startswith("-"):
            raise UsageError(f"fastro make {generator.name} doesn't take {token!r}.")
        key = taken[flag][0].name if flag in taken else flag
        if key in seen:
            raise UsageError(f"{token} is given twice.")
        seen.add(key)
        if flag == OUTPUT[1]:
            own["output"] = _value(flag, equals, inline, rest)
        elif flag in (FORCE[1], YES[1], DRY_RUN, HELP):
            if equals:
                raise UsageError(f"{flag} takes no value.")
            own[flag] = True
        elif flag in taken:
            param, setting = taken[flag]
            if setting is not None:
                if equals:
                    raise UsageError(f"{flag} takes no value.")
                given[param.name] = setting
            else:
                given[param.name] = _checked(param, flag, _value(flag, equals, inline, rest))
        else:
            raise UsageError(f"fastro make {generator.name} doesn't take {flag}. Run fastro make {generator.name} --help.")
    values = {name: given.get(name, param.default) for name, param in generator.params.items()}
    return Request(
        generator,
        mode,
        values,
        output=str(own["output"]) if "output" in own else None,
        force=FORCE[1] in own or YES[1] in own,
        dry_run=DRY_RUN in own,
        help=HELP in own,
    )


def _value(flag: str, equals: str, inline: str, rest: list[str]) -> str:
    if equals:
        return inline
    if not rest:
        raise UsageError(f"{flag} takes a value.")
    return rest.pop(0)


def _checked(param: Param, flag: str, text: str) -> str | int:
    try:
        return param_value(param, text)
    except SpecError as error:
        raise UsageError(f"{flag}: {error}") from None


def usage(catalog: Blueprint | None, request: Request | None = None) -> list[str]:
    """The help ``fastro make`` prints: its generators, or one generator's modes and flags."""
    if request is None:
        lines = ["Usage: fastro make GENERATOR [MODE] [OPTIONS]", "", "Render one of the blueprint's generators into the repo."]
        if catalog is None:
            return [*lines, "", "Run it in a blueprint repo to see the generators its blueprint declares."]
        generators = [generator for name, generator in catalog.generators.items() if name != MODULE_GENERATOR]
        rows = [(generator.name, ", ".join(generator.modes)) for generator in generators]
        return [*lines, "", "Generators:", *_rows(rows)] if rows else [*lines, "", "This blueprint declares no generators."]
    generator = request.generator
    mode = " MODE" if generator.modes else ""
    rows = []
    for flag, (param, setting) in flags(generator).items():
        if setting is False:
            continue
        shown = f"{flag} / --no-{flag[2:]}" if setting else f"{flag} {param.type.upper()}"
        default = (flag if param.default else f"--no-{flag[2:]}") if setting else param.default
        rows.append((shown, f"{param.help} (default: {default})".lstrip() if default != "" else param.help))
    rows += [
        ("-o, --output-dir DIR", "Where to write the files. Defaults to the repo root."),
        ("-f, --force", "Replace a file that differs without asking."),
        ("-y, --yes", "The same as --force."),
        ("--dry-run", "Write nothing; name each file make would write."),
    ]
    modes = ["", f"Modes: {', '.join(generator.modes)}"] if generator.modes else []
    return [f"Usage: fastro make {generator.name}{mode} [OPTIONS]", *modes, "", "Options:", *_rows(rows)]


def _rows(rows: Iterable[tuple[str, str]]) -> list[str]:
    listed = list(rows)
    width = max(len(left) for left, _ in listed)
    return [f"  {left.ljust(width)}  {right}".rstrip() for left, right in listed]


def context(source: Source, selection: Selection, values: Mapping[str, Any], version: str) -> dict[str, Any]:
    """The template context of spec 6.2 for ``source``'s selection and the param ``values``."""
    services: set[str] = set()
    for feature in source.catalog.features:
        if feature.name not in selection.features:
            continue
        services.update(feature.services)
        for option in feature.options.values():
            services.update(option.services.get(selection.options[(feature.name, option.name)], ()))
        for when in feature.when:
            if selection.holds(when.condition):
                services.update(when.services)
    return {
        "project": {"name": source.project.name, "slug": source.project.slug},
        "blueprint": {"name": source.catalog.info.name, "version": str(source.catalog.info.version)},
        "fastro": {"version": version},
        "features": list(selection.features),
        "options": {f"{feature}.{option}": value for (feature, option), value in selection.options.items()},
        "services": sorted(services),
        "params": dict(values),
        "has": _has(source.catalog, selection),
    }


def _has(catalog: Blueprint, selection: Selection) -> Callable[[object], bool]:
    features = {feature.name: feature for feature in catalog.features}

    def has(text: object) -> bool:
        match = conditions.ATOM.fullmatch(text) if isinstance(text, str) else None
        if match is None or match[1] in OPERATORS:
            raise SpecError(f"has({text!r}): has takes one feature name or 'feature.option=value'.")
        name, option, value = match.groups()
        if name not in features:
            raise SpecError(f"has({text!r}): {name!r} isn't a feature of this blueprint.")
        if option is None:
            return selection.holds(conditions.Feature(name))
        options = features[name].options
        if option not in options:
            raise SpecError(f"has({text!r}): '{name}.{option}' isn't an option of {name}.")
        if value not in options[option].choices:
            raise SpecError(f"has({text!r}): {value!r} isn't a choice of {name}.{option}.")
        return selection.holds(conditions.Option(name, option, value))

    return has


def render(root: Path, request: Request, tracked: Iterable[str], values: Mapping[str, Any]) -> list[File]:
    """Every file the request writes, read and rendered, by path, before anything is written.

    Raises:
        Refused: the tree holds no tracked file, two files would be written at one path, a template is one spec 1.10
            rules out or doesn't render.
        OSError: the system won't let fastro read a template.
    """
    generator = request.generator
    base = generator.templates.text.rstrip("/") + "/" + (f"{request.mode}/" if request.mode else "")
    sources = sorted(path for path in tracked if path.startswith(base))
    if not sources:
        raise Refused(f"{base} holds no tracked file, so fastro make {generator.name} has nothing to write.")
    found: dict[str, File] = {}
    environment = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)
    for template in sources:
        path = template[len(base) :].removesuffix(TEMPLATE) if template.endswith(TEMPLATE) else template[len(base) :]
        if path in found or path == "" or path.endswith("/"):
            other = f" and {found[path].template}" if path in found else ""
            raise Refused(f"{template}{other} would be written at the same path, so nothing was written.")
        if (reason := breaks_the_rule(root, template)) is not None:
            raise Refused(f"{template} {reason}. fastro make reads templates only as regular files inside the repo.")
        data = (root / template).read_bytes()
        if template.endswith(TEMPLATE):
            data = _rendered(environment, template, data, values)
        found[path] = File(path, template, data)
    return list(found.values())


def _rendered(environment: Environment, template: str, data: bytes, values: Mapping[str, Any]) -> bytes:
    try:
        text = data.removeprefix(codecs.BOM_UTF8).decode("utf-8")
        return environment.from_string(text).render(values).encode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"{template} isn't UTF-8 text, so it can't be rendered.") from None
    except Exception as error:
        raise Refused(f"{template} doesn't render: {error}") from None


def check(output: Path, files: Iterable[File]) -> None:
    """Refuse the run when a file can't be written where it goes (spec 2.9).

    Raises:
        Refused: the output directory isn't one, or an output path isn't a regular file, has a symlink at it or
            between it and the output directory, or has a file where one of its directories would be.
        OSError: the system won't let fastro look.
    """
    kind = file_kind(output)
    if kind not in (None, "directory"):
        raise Refused(f"{output} isn't a directory, so fastro make can't write in it.")
    for file in files:
        current = output
        parts = file.path.split("/")
        for depth, part in enumerate(parts):
            current = current / part
            kind = file_kind(current, follow=False)
            last = depth == len(parts) - 1
            if kind is None:
                break
            if kind == "symlink":
                raise Refused(f"{current} is a symlink, and fastro make writes nothing through one.")
            if last and kind != "file":
                raise Refused(f"{current} isn't a regular file, so fastro make won't replace it.")
            if not last and kind != "directory":
                raise Refused(f"{current} isn't a directory, so fastro make can't write {file.path} under it.")
