"""Rendering the wiring a selection gets (spec 2.6 to 2.8)."""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fastro.engine.blueprint import Blueprint, Info, Slot, Wiring
from fastro.engine.resolve import APP, Taken
from fastro.engine.spec import Ref, RepoPath

CORE = "core"
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
                raise ValueError(f"{line!r} isn't an import statement.")
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


def render_python(catalog: Blueprint, file: Wiring, found: Mapping[str, tuple[Taken, ...]]) -> str:
    """The text of the Python wiring ``file``, from each point's ordered contributions in ``found`` (2.6, 2.8).

    ``found`` is what ``ordered`` returns for a selection that has passed ``check_one_points`` and
    ``check_value_slots``.
    """
    imports = list(file.imports)
    slots = []
    for slot in file.slots:
        text, needed = _ASSIGNMENTS[slot.kind](catalog, file, slot, found.get(slot.point, ()))
        slots.append(text)
        imports += needed
    block = import_block(imports, catalog.info.import_root, catalog.line_length)
    return _docstring(file.doc, catalog.header) + "\n" + (block + "\n" if block else "") + "".join(slots)


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
    ref = _ref(contribution.target)
    fields.update(name=ref.alias or ref.attr, ref=str(ref), module=ref.module, attr=ref.attr)
    template = slot.item or ("{name}()" if contribution.call else "{name}")
    return template.format(**fields), [ref_import(ref, file.path, catalog.info)]


def _ref(target: object) -> Ref:
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
        ref = _ref(items[0].contribution.target)
        return f"{slot.name}: {slot.annotation} = {ref.alias or ref.attr}\n", [ref_import(ref, file.path, catalog.info)]
    if slot.fallback is None:
        raise ValueError(f"{slot.name} has no contribution and no fallback.")
    return f"{slot.name}: {slot.annotation} = {slot.fallback}\n", list(slot.fallback_imports)


_ASSIGNMENTS = {"tuple": _tuple, "list": _list, "value": _value}
