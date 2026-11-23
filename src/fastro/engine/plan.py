"""Planning what a project gets from its blueprint: one op per path, with its origin and reason (spec 7)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from fastro.engine.blueprint import Blueprint
from fastro.engine.lock import Lock
from fastro.engine.resolve import APP, Owner, Ownership, Selection
from fastro.engine.spec import RepoPath

PLANNED_ORIGINS = ("core", "wiring")
LOCK = "uv.lock"
DELETED = "in the previous lock, and not in this plan"
APP_CODE = "it's the project's own code"


@dataclass(frozen=True)
class Op:
    """What the plan does with one path (spec 7): ``copy``, ``skip``, ``render`` or ``delete``."""

    op: str
    path: str
    origin: str
    reason: str


@dataclass(frozen=True)
class Plan:
    """Every op for a selection, sorted by path, with the catalog and selection it was made from."""

    catalog: Blueprint
    selection: Selection
    ops: tuple[Op, ...]

    def origins(self) -> dict[str, str]:
        """The ``origin`` the lock records for each file this plan copies or renders (spec 4): its op's, except
        ``deps`` for ``{python_root}/pyproject.toml`` and ``env`` for ``{python_root}/.env.example``, which are
        rewritten whoever owns them. ``{python_root}/uv.lock`` is the project's, and gets none."""
        rewritten = {
            in_python_root(self.catalog, "pyproject.toml"): "deps",
            in_python_root(self.catalog, ".env.example"): "env",
        }
        return {
            op.path: rewritten.get(op.path, op.origin)
            for op in self.ops
            if op.op in ("copy", "render") and op.path != python_root_lock(self.catalog)
        }

    def document(self) -> dict[str, Any]:
        """The plan as spec 7's JSON document, with the parts nothing fills yet present and empty."""
        info = self.catalog.info
        return {
            "schema": 1,
            "blueprint": {"name": info.name, "version": str(info.version)},
            "selection": {
                "features": list(self.selection.features),
                "options": {f"{feature}.{option}": value for (feature, option), value in self.selection.options.items()},
                "plugins": [],
                "modules": [],
            },
            "ops": [{"op": op.op, "path": op.path, "origin": op.origin, "reason": op.reason} for op in self.ops],
            "dependencies": {"add": [], "remove": []},
            "migrations": {"drops": [], "adds": []},
            "next": [],
        }


def plan(
    catalog: Blueprint,
    selection: Selection,
    files: Iterable[RepoPath],
    app_paths: Iterable[RepoPath] = (),
    previous: Lock | None = None,
) -> Plan:
    """The plan for ``selection`` over the source tree's ``files`` (spec 7, rev 1.17).

    Every path gets one op. A file ``previous`` locked that this plan doesn't copy or render is a ``delete``,
    unless it's ``[app]`` code, which is skipped and never deleted. Only files a plan makes are considered, so a
    locked file from another step keeps no op (rev 1.19).
    """
    ownership = Ownership(catalog, app_paths)
    sources = {path.text: path for path in files}
    ops: dict[str, Op] = {}
    for file in catalog.wiring:
        text = file.path.text
        if file.condition is None or selection.holds(file.condition):
            reason = f"generated wiring, since {file.condition_text} holds" if file.condition_text else "generated wiring"
            ops[text] = Op("render", text, "wiring", reason)
        elif text in sources:
            ops[text] = Op("skip", text, "wiring", f"the wiring file's if, {file.condition_text}, doesn't hold")
    for text, path in sources.items():
        if text not in ops:
            ops[text] = _copy_or_skip(catalog, selection, path, ownership.of(path))
    for path, locked in previous.files.items() if previous is not None else ():
        kept = path.text in ops and ops[path.text].op in ("copy", "render")
        if kept or not (locked.origin in PLANNED_ORIGINS or locked.origin.startswith("feature:")):
            continue
        if ownership.of(path).app:
            ops.setdefault(path.text, Op("skip", path.text, APP, APP_CODE))
        else:
            ops[path.text] = Op("delete", path.text, locked.origin, DELETED)
    return Plan(catalog, selection, tuple(ops[text] for text in sorted(ops)))


def _copy_or_skip(catalog: Blueprint, selection: Selection, path: RepoPath, owner: Owner) -> Op:
    text = path.text
    if any(only.covers(text) for only in catalog.info.all_blueprint_only):
        return Op("skip", text, owner.origin, "it's blueprint_only")
    if owner.app:
        return Op("skip", text, owner.origin, APP_CODE)
    if not owner.feature:
        return Op("copy", text, owner.origin, "no feature owns it")
    if owner.feature not in selection.pulled_by:
        return Op("skip", text, owner.origin, f"feature {owner.feature} isn't selected")
    if owner.when is None:
        return Op("copy", text, owner.origin, f"feature {owner.feature} is selected")
    when = next(feature for feature in catalog.features if feature.name == owner.feature).when[owner.when]
    if selection.holds(when.condition):
        return Op("copy", text, owner.origin, f"feature {owner.feature} is selected and {when.condition_text} holds")
    return Op("skip", text, owner.origin, f"{when.condition_text} doesn't hold")


def starting_lock(catalog: Blueprint, tracked: Sequence[RepoPath]) -> RepoPath | None:
    """The lock a build or a new project starts from (spec 2.13, 4): ``{python_root}/uv.lock`` when it's among
    ``tracked``, otherwise a ``uv.lock`` at the root among them, otherwise none."""
    names = {path.text for path in tracked}
    for candidate in (python_root_lock(catalog), LOCK):
        if candidate in names:
            return RepoPath(candidate)
    return None


def python_root_lock(catalog: Blueprint) -> str:
    """Where a project's lock sits: ``{python_root}/uv.lock``."""
    return in_python_root(catalog, LOCK)


def in_python_root(catalog: Blueprint, name: str) -> str:
    """The path of ``name`` in ``catalog``'s ``python_root``, however the root is written."""
    return f"{catalog.info.python_root.text.rstrip('/')}/{name}"
