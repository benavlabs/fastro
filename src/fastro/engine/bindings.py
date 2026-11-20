"""The names a module binds at module level, as FR014 reads them (spec 2.13), with the import each one comes from."""

from __future__ import annotations

import ast

STAR = "*"

Source = tuple[object, ...] | None


def bindings(tree: ast.Module) -> list[tuple[str, Source]]:
    """Each name ``tree`` binds at module level, in no particular order, with the import it comes from:
    ``("from", level, module, name)`` or ``("import", name)``, or ``None`` for any other statement. A star import
    gives ``*``.

    A ``def``, ``async def`` or ``class``; an assignment, augmented or annotated assignment to a name or a tuple, list
    or starred target; an ``import`` or ``from ... import``; and any of these inside ``if``, ``with``, ``try`` and
    ``except`` blocks, to any depth, bind a name (2.13).
    """
    found: list[tuple[str, Source]] = []
    pending: list[ast.stmt] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.append((node.name, None))
        elif isinstance(node, ast.Assign):
            found += [(name, None) for target in node.targets for name in _targets(target)]
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            found += [(name, None) for name in _targets(node.target)]
        elif isinstance(node, ast.Import):
            found += [(alias.asname or alias.name.split(".")[0], ("import", alias.name)) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found += [
                (STAR if alias.name == STAR else alias.asname or alias.name, ("from", node.level, node.module, alias.name))
                for alias in node.names
            ]
        elif isinstance(node, ast.If | ast.With | ast.AsyncWith):
            pending += [*node.body, *(node.orelse if isinstance(node, ast.If) else [])]
        elif isinstance(node, ast.Try | ast.TryStar):
            pending += [*node.body, *node.orelse, *node.finalbody]
            pending += [statement for handler in node.handlers for statement in handler.body]
    return found


def _targets(target: ast.expr) -> list[str]:
    """The names an assignment target binds: a name, or the names a tuple, list or starred target unpacks into."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _targets(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _targets(element)]
    return []
