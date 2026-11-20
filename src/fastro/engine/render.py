"""Rendering the wiring a selection gets (spec 2.6 to 2.8)."""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from fastro.engine import bindings
from fastro.engine.blueprint import Blueprint, Contribution, Contributions, Info, Slot, Wiring
from fastro.engine.plan import Plan
from fastro.engine.positions import KeyPath
from fastro.engine.resolve import APP, Taken, check_fallback_slots, check_one_points, taken
from fastro.engine.spec import Ref, RepoPath

CORE = "core"
STAND_IN = "_fastro_app_"
FUTURE, STDLIB, THIRD_PARTY, FIRST_PARTY, LOCAL = range(5)
FIRST_PARTY_EXTRA = ("scripts", "tests")


def feature_indexes(catalog: Blueprint) -> dict[str, int]:
    """Each owner's ``feature_index`` (2.7): ``core`` is -1, then each feature in catalog order, and ``app`` last."""
    indexes = {CORE: -1}
    indexes.update({f"feature:{feature.name}": index for index, feature in enumerate(catalog.features)})
    indexes[APP] = len(catalog.features)
    return indexes


def ordered(catalog: Blueprint, found: Mapping[str, tuple[Taken, ...]]) -> dict[str, tuple[Taken, ...]]:
    """Each point's contributions sorted by ``(order, feature_index, position)``, keeping ties stable (2.7).

    A contribution with no ``order`` gets ``(feature_index + 1) * 100 + position``.
    """
    indexes = feature_indexes(catalog)

    def key(item: Taken) -> tuple[int, int, int]:
        index = indexes[item.owner]
        order = item.contribution.order
        return (order if order is not None else (index + 1) * 100 + item.position, index, item.position)

    return {point: tuple(sorted(items, key=key)) for point, items in found.items()}


def ref_import(ref: Ref, file: RepoPath, info: Info) -> str:
    """The import line a ref renders as in ``file`` (2.8).

    In a file under ``{python_root}/{import_root}/``, a module that is ``import_root`` or sits under it is imported
    relative to the file's package: one dot for the package, and one more for each level up. Anything else is absolute.
    """
    root = info.python_root.text.removesuffix("/") + "/"
    parts = ref.module.split(".")
    module = ref.module
    if file.text.startswith(f"{root}{info.import_root}/") and parts[0] == info.import_root:
        package = file.text[len(root) :].split("/")[:-1]
        common = 0
        while common < min(len(package), len(parts)) and package[common] == parts[common]:
            common += 1
        module = "." * (1 + len(package) - common) + ".".join(parts[common:])
    name = f"{ref.attr} as {ref.alias}" if ref.alias else ref.attr
    return f"from {module} import {name}"


class RenderError(ValueError):
    """A wiring file this catalog and selection can't render: a problem in the catalog, not in fastro."""


@dataclass(frozen=True)
class _Import:
    """One statement: ``import module [as alias]`` when ``names`` is empty, else ``from module import names``."""

    module: str
    level: int = 0
    names: tuple[tuple[str, str | None], ...] = ()
    alias: str | None = None


def import_block(lines: Iterable[str], import_root: str, line_length: int) -> str:
    """The import block for a generated file (2.8), or ``""`` when there are no imports.

    Names imported plainly from one module are merged and sorted by type; aliased ones stay one per statement. The
    statements are sectioned (``__future__``, standard library, third party, first party, local) with a blank line
    between, plain imports first in each, then by module, and statements from one module by their first name.
    """
    plain: dict[tuple[str, str | None], None] = {}
    merged: dict[tuple[int, str], dict[str, None]] = {}
    aliased: dict[tuple[int, str, str, str], None] = {}
    for line in lines:
        for node in ast.parse(line).body:
            if isinstance(node, ast.Import):
                plain.update(((alias.name, alias.asname), None) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                key = (node.level, node.module or "")
                for alias in node.names:
                    if alias.asname is None:
                        merged.setdefault(key, {})[alias.name] = None
                    else:
                        aliased[(*key, alias.name, alias.asname)] = None
            else:
                raise RenderError(f"{line!r} isn't an import statement.")
    statements = [_Import(module, alias=alias) for module, alias in plain]
    statements += [
        _Import(module, level, tuple((name, None) for name in sorted(names, key=_member)))
        for (level, module), names in merged.items()
    ]
    statements += [_Import(module, level, ((name, alias),)) for level, module, name, alias in aliased]
    sections: dict[int, list[_Import]] = {}
    for statement in statements:
        sections.setdefault(_section(statement, import_root), []).append(statement)
    return "\n".join(
        "\n".join(_line(statement, line_length) for statement in sorted(sections[section], key=_order)) + "\n"
        for section in sorted(sections)
    )


def _natural(text: str) -> tuple[str | int, ...]:
    """``text`` as ruff orders it: a run of digits compares as a number, so ``r2`` sorts before ``r10``."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text))


def _member(name: str) -> tuple[int, tuple[str | int, ...], tuple[str | int, ...]]:
    """isort's ``order-by-type``: CONSTANTS, then Classes, then everything else, each compared without regard to
    case first and then with it, as ruff's ``case-sensitive = false`` does (2.8)."""
    kind = 0 if len(name) > 1 and name.isupper() else 1 if name[:1].isupper() else 2
    return (kind, _natural(name.lower()), _natural(name))


def _section(statement: _Import, import_root: str) -> int:
    if statement.level:
        return LOCAL
    top = statement.module.split(".")[0]
    if top == "__future__":
        return FUTURE
    if top in sys.stdlib_module_names:
        return STDLIB
    return FIRST_PARTY if top in (import_root, *FIRST_PARTY_EXTRA) else THIRD_PARTY


def _order(statement: _Import) -> tuple[object, ...]:
    if statement.names:
        first: tuple[object, ...] = (1, *_member(statement.names[0][0]))
    else:
        first = (0,) if statement.alias is None else (1, *_member(statement.alias))
    return (bool(statement.names), -statement.level, _natural(statement.module.lower()), statement.module, first)


def _line(statement: _Import, line_length: int) -> str:
    if not statement.names:
        return f"import {statement.module}" + (f" as {statement.alias}" if statement.alias else "")
    module = "." * statement.level + statement.module
    names = [f"{name} as {alias}" if alias else name for name, alias in statement.names]
    line = f"from {module} import {', '.join(names)}"
    if len(line) <= line_length:
        return line
    return f"from {module} import (\n" + "".join(f"    {name},\n" for name in names) + ")"


def render(plan: Plan, app: Contributions | None = None) -> dict[str, str]:
    """The text of each wiring file ``plan`` renders, by path in the plan's order (2.6 to 2.8).

    The contributions are the catalog's and ``app``'s (the project's ``[app]``) that the plan's selection takes. A
    ``one`` point given more than one, or a value or reexport slot with no fallback given none, is refused first.
    """
    catalog, selection = plan.catalog, plan.selection
    found = taken(catalog, selection, app)
    check_one_points(catalog, found)
    check_fallback_slots(catalog, selection, found)
    found = ordered(catalog, found)
    files = {file.path.text: file for file in catalog.wiring}
    return {
        op.path: (render_data if files[op.path].slots[0].kind == "data" else render_python)(catalog, files[op.path], found)
        for op in plan.ops
        if op.op == "render"
    }


def shadows(plan: Plan, app: Contributions, line: Callable[[KeyPath], int]) -> list[tuple[KeyPath, str]]:
    """Each ``app`` ref whose name a Python wiring file it's rendered into binds otherwise, as FR014 reads a binding,
    with where ``fastro.toml`` writes it and why: of two app refs that bind one name, the later one, by the ``line``
    of ``fastro.toml`` each is written on and then by declaration order (3).

    What a file binds otherwise is what it binds when each app ref is imported from a module of its own, so the
    slots render as they will. An import the import block merges with the ref's own isn't another binding. A
    ``reexport`` slot binds its own name, and a ``literal`` or ``data`` slot takes no refs.
    """
    catalog = plan.catalog
    kept = {slot.point for file in catalog.wiring for slot in file.slots if slot.literal or slot.kind in ("data", "reexport")}
    entries = [(point, item) for point, items in app.items() for item in items]
    order = {id(item): (line(item.at), index) for index, (_, item) in enumerate(entries)}
    stand_ins: dict[str, list[Contribution]] = {}
    for index, (point, item) in enumerate(entries):
        if point not in kept:
            ref = as_ref(item.target)
            item = replace(item, target=Ref(f"{STAND_IN}{index}", ref.attr, ref.alias))
        stand_ins.setdefault(point, []).append(item)
    rendered = render(plan, {point: tuple(items) for point, items in stand_ins.items()})
    found = taken(catalog, plan.selection, app)
    problems = []
    for file in catalog.wiring:
        if file.path.text not in rendered or file.slots[0].kind == "data":
            continue
        refs = sorted(
            (
                (order[id(item.contribution)], item.contribution.at, as_ref(item.contribution.target))
                for slot in file.slots
                if not slot.literal and slot.kind != "reexport"
                for item in found.get(slot.point, ())
                if item.owner == APP
            ),
            key=lambda entry: entry[0],
        )
        otherwise: dict[str, set[bindings.Source]] = {}
        for name, source in bindings.bindings(ast.parse(rendered[file.path.text])):
            if not (source and source[0] == "from" and str(source[2]).startswith(STAND_IN)):
                otherwise.setdefault(name, set()).add(source)
        first: dict[str, tuple[bindings.Source, Ref]] = {}
        for _, at, ref in refs:
            name, source = bindings.bindings(ast.parse(ref_import(ref, file.path, catalog.info)))[0]
            if otherwise.get(name, {source}) - {source}:
                problems.append((at, f"{str(ref)!r} binds {name!r} in {file.path.text}, which that file binds otherwise too."))
            elif name in first and first[name][0] != source:
                earlier = str(first[name][1])
                problems.append(
                    (at, f"{str(ref)!r} binds {name!r} in {file.path.text}, which the app ref {earlier!r} binds too.")
                )
            first.setdefault(name, (source, ref))
    return problems


def render_python(catalog: Blueprint, file: Wiring, found: Mapping[str, tuple[Taken, ...]]) -> str:
    """The text of the Python wiring ``file``, from each point's ordered contributions in ``found`` (2.6, 2.8).

    ``found`` is what ``ordered`` returns for a selection that has passed ``check_one_points`` and
    ``check_fallback_slots``.
    """
    imports = list(file.imports)
    pieces = []
    for slot in file.slots:
        text, needed = _SLOTS[slot.kind](catalog, file, slot, found.get(slot.point, ()))
        pieces.append((text, _spaced(slot)))
        imports += needed
    block = import_block(imports, catalog.info.import_root, catalog.line_length)
    rendered = _docstring(file.doc, catalog.header) + "\n" + (block + "\n" if block else "")
    for index, (text, spaced) in enumerate(pieces):
        if spaced or (index and pieces[index - 1][1]):
            rendered += "\n" if index == 0 else "\n\n"
        rendered += text
    return rendered


def _spaced(slot: Slot) -> bool:
    """Whether ``slot`` gets two blank lines on each side: a ``class``, or ``raw`` code that starts with a ``def``, an
    ``async def``, a ``class`` or a decorator (2.8)."""
    if slot.kind == "class":
        return True
    body = ast.parse(slot.code).body if slot.kind == "raw" else []
    return bool(body) and isinstance(body[0], ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)


def _docstring(doc: str, header: str) -> str:
    """The module docstring: ``doc``, then the header after a blank line, each with its outer newlines stripped (2.8)."""
    doc, header = doc.strip("\n"), header.strip("\n")
    return f'"""{doc}\n\n{header}\n"""\n' if header else f'"""{doc}"""\n'


def _item(catalog: Blueprint, file: Wiring, slot: Slot, item: Taken) -> tuple[str, list[str]]:
    """One entry of a ``tuple`` or ``list`` slot, and the import it needs (2.6)."""
    contribution = item.contribution
    fields: dict[str, object] = {**slot.defaults, **contribution.fields}
    if slot.literal:
        return (slot.item or '"{value}"').format(**fields, value=contribution.target), []
    ref = as_ref(contribution.target)
    fields.update(name=ref.alias or ref.attr, ref=str(ref), module=ref.module, attr=ref.attr)
    template = slot.item or ("{name}()" if contribution.call else "{name}")
    return template.format(**fields), [ref_import(ref, file.path, catalog.info)]


def as_ref(target: object) -> Ref:
    """A contribution's target as a ref: parsed already for a ``ref`` table, or parsed here from a bare string."""
    return target if isinstance(target, Ref) else Ref.parse(str(target))


def _collection(catalog: Blueprint, file: Wiring, slot: Slot, items: tuple[Taken, ...], brackets: str) -> tuple[str, list[str]]:
    """A ``tuple`` or ``list`` slot in ``brackets``, with its item imports only when it has entries (2.6)."""
    entries = [_item(catalog, file, slot, item) for item in items]
    imports = [line for _, needed in entries for line in needed] + (list(slot.item_imports) if entries else [])
    texts = [text for text, _ in entries]
    start = f"{slot.name}: {slot.annotation} = {brackets[0]}"
    if not texts:
        return f"{start}{brackets[1]}\n", imports
    if brackets == "()" and len(texts) == 1:
        return f"{start}{texts[0]},)\n", imports
    return start + "\n" + "".join(f"    {text},\n" for text in texts) + f"{brackets[1]}\n", imports


def _tuple(catalog: Blueprint, file: Wiring, slot: Slot, items: tuple[Taken, ...]) -> tuple[str, list[str]]:
    """``NAME: ANN = (...)``: one line for one entry, exploded for more (2.6)."""
    return _collection(catalog, file, slot, items, "()")


def _list(catalog: Blueprint, file: Wiring, slot: Slot, items: tuple[Taken, ...]) -> tuple[str, list[str]]:
    """``NAME: ANN = [...]``, always exploded (2.6)."""
    return _collection(catalog, file, slot, items, "[]")


def _value(catalog: Blueprint, file: Wiring, slot: Slot, items: tuple[Taken, ...]) -> tuple[str, list[str]]:
    """``NAME: ANN = name`` for its one contribution, or ``= FALLBACK`` with its imports when there's none (2.6)."""
    if items:
        ref = as_ref(items[0].contribution.target)
        return f"{slot.name}: {slot.annotation} = {ref.alias or ref.attr}\n", [ref_import(ref, file.path, catalog.info)]
    if slot.fallback is None:
        raise RenderError(f"{slot.name} has no contribution and no fallback.")
    return f"{slot.name}: {slot.annotation} = {slot.fallback}\n", list(slot.fallback_imports)


def _class(catalog: Blueprint, file: Wiring, slot: Slot, items: tuple[Taken, ...]) -> tuple[str, list[str]]:
    """``class NAME(bases):`` and its docstring: the contributions, or else the fallback, then ``append`` (2.6).

    The bases go on one line when it fits in the line length, and one per line with a trailing comma when it doesn't.
    """
    refs = [as_ref(item.contribution.target) for item in items]
    if not refs and slot.fallback is not None:
        refs = [as_ref(slot.fallback)]
    refs += slot.append
    names = [ref.name for ref in refs]
    line = f"class {slot.name}({', '.join(names)}):" if names else f"class {slot.name}:"
    if names and len(line) > catalog.line_length:
        line = f"class {slot.name}(\n" + "".join(f"    {name},\n" for name in names) + "):"
    return f"{line}\n{_class_doc(slot.doc)}", [ref_import(ref, file.path, catalog.info) for ref in refs]


def _class_doc(doc: str) -> str:
    """A class docstring, indented: one line between its quotes, or several with the closing quotes on their own."""
    lines = doc.strip("\n").split("\n")
    if len(lines) == 1:
        return f'    """{lines[0]}"""\n'
    rest = "".join((f"    {line}" if line else "") + "\n" for line in lines[1:])
    return f'    """{lines[0]}\n{rest}    """\n'


def _reexport(catalog: Blueprint, file: Wiring, slot: Slot, items: tuple[Taken, ...]) -> tuple[str, list[str]]:
    """``__all__ = ["NAME"]``, with ``from M import ATTR as NAME`` for the import block: the contribution, or else the
    fallback, and no ``as NAME`` when ATTR is NAME (2.6)."""
    if items:
        ref = as_ref(items[0].contribution.target)
    elif slot.fallback is not None:
        ref = as_ref(slot.fallback)
    else:
        raise RenderError(f"{slot.name} has no contribution and no fallback.")
    bound = Ref(ref.module, ref.attr, None if ref.attr == slot.name else slot.name)
    return f'__all__ = ["{slot.name}"]\n', [ref_import(bound, file.path, catalog.info)]


def _raw(catalog: Blueprint, file: Wiring, slot: Slot, items: tuple[Taken, ...]) -> tuple[str, list[str]]:
    """``code`` verbatim, without its outer newlines (2.6)."""
    return slot.code.strip("\n") + "\n", []


_SLOTS = {"tuple": _tuple, "list": _list, "value": _value, "class": _class, "reexport": _reexport, "raw": _raw}


def render_data(catalog: Blueprint, file: Wiring, found: Mapping[str, tuple[Taken, ...]]) -> str:
    """The text of the data wiring ``file`` (2.6.1): a ``ts`` file's comment and one export per slot, or a ``json``
    file's object of every slot, with each item its ``data`` table followed by the owner as ``"feature"``."""
    lists = {slot.name: [_data_item(item) for item in found.get(slot.point, ())] for slot in file.slots}
    if file.slots[0].format == "json":
        return json.dumps(lists, indent=2, ensure_ascii=False) + "\n"
    lines = _comment(file.doc)
    if catalog.header.strip("\n"):
        lines += ["//", *_comment(catalog.header)]
    exports = "".join(f"export const {name} = {_inline(items)} as const;\n" for name, items in lists.items())
    return "\n".join(lines) + "\n\n" + exports


def _data_item(item: Taken) -> dict[str, object]:
    """A contribution's ``data`` table, in the order written, then ``"feature"``: the feature, ``core`` or ``app``."""
    target = item.contribution.target
    assert isinstance(target, Mapping)
    return {**target, "feature": item.owner.removeprefix("feature:")}


def _comment(text: str) -> list[str]:
    """Each line of ``text``, its outer newlines stripped, as a ``//`` comment, with a bare ``//`` for an empty one."""
    return [f"// {line}" if line else "//" for line in text.strip("\n").split("\n")]


def _inline(value: object) -> str:
    """``value`` as JSON on one line: ``{ "k": v }`` for a table, ``[a, b]`` for a list, ``[]`` and ``{}`` when empty."""
    if isinstance(value, Mapping):
        pairs = ", ".join(f"{json.dumps(key, ensure_ascii=False)}: {_inline(item)}" for key, item in value.items())
        return f"{{ {pairs} }}" if pairs else "{}"
    if isinstance(value, list):
        return "[" + ", ".join(_inline(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)
