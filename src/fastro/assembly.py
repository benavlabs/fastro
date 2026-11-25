"""What a selection gives a project from a blueprint's files, shared by a ``blueprint verify`` build and ``fastro new``
(spec 2.13, 4, 7): the plan's copies, the wiring it renders, the overlay, the rewritten ``pyproject.toml`` and
``.env.example``, and the lock the project starts from, with the ``[files]`` origin of each file the lock records."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from fastro import overlay, pyproject, templates
from fastro.apply import Change, changes
from fastro.engine import envfile
from fastro.engine.blueprint import Blueprint
from fastro.engine.files import breaks_the_rule, shown, utf8
from fastro.engine.plan import in_python_root, plan, python_root_lock, starting_lock
from fastro.engine.render import RenderError, render
from fastro.engine.resolve import Selection, SelectionError
from fastro.engine.spec import RepoPath
from fastro.tools import installed_version

OVERLAY = "overlay"

Marked = Callable[[Sequence[RepoPath]], Sequence[RepoPath]]


class Unbuildable(Exception):
    """The catalog loaded, but a build of it can't be rendered: a problem in ``blueprint.toml`` (spec 8: exit 1)."""


@dataclass(frozen=True)
class Assembled:
    """The changes that give a project its files, and the ``[files]`` origin (spec 4) of each one the lock records."""

    changes: list[Change]
    origins: dict[str, str]


def assemble(
    root: Path, catalog: Blueprint, selection: Selection, files: Sequence[RepoPath], name: str, marked: Marked
) -> Assembled:
    """What ``selection`` gives a project named ``name`` from ``files`` under ``root``: what the plan copies, the
    wiring it renders, the overlay (``overlay.files``), its ``{python_root}/pyproject.toml`` and
    ``{python_root}/.env.example`` rewritten for the selection (6.3, 6.1), whichever source gives them, and the lock
    it starts from as ``{python_root}/uv.lock`` (4). ``marked`` gives the files among those it's handed that git's
    ``export-subst`` attribute is set on. Nothing is read past a refusal, and nothing is written.

    The origins are the plan's for each file it copies or renders, ``overlay`` for an overlay file at its project
    path, and ``deps`` and ``env`` for the two rewritten paths, whether or not the build has them; the starting lock
    has none.

    Raises:
        Unbuildable: the wiring can't be rendered, an overlay file can't be given (``overlay.OverlayError``) or would
            land where another source does or under or over one, a file it would copy is at a path that isn't UTF-8,
            ``marked`` names one, the selection's runtime dependencies can't be merged, or ``.env.example`` has a
            problem 6.1 refuses.
    """
    made = plan(catalog, selection, files)
    try:
        rendered = render(made)
    except (RenderError, SelectionError) as error:
        raise Unbuildable(str(error)) from None
    found = changes(made, rendered)
    lock = starting_lock(catalog, files)
    if lock is not None:
        found = [change for change in found if change.path != python_root_lock(catalog)]
        found.append(Change("copy", python_root_lock(catalog), source=lock.text))
    values = templates.context(catalog, selection, name, str(installed_version()))
    try:
        overlaid = overlay.files(root, catalog, files, values)
        overlay.check(
            found, overlaid, (op.path for op in made.ops if op.op == "render"), {op.path: op.origin for op in made.ops}
        )
    except overlay.OverlayError as error:
        raise Unbuildable(str(error)) from None
    copied = [RepoPath(change.source or change.path) for change in found if change.kind == "copy"]
    copied += [RepoPath(source) for source, _ in overlaid]
    found += [change for _, change in overlaid]
    if odd := next((path.text for path in copied if not utf8(path.text)), None):
        raise Unbuildable(
            f"{shown(odd)} is at a path that isn't UTF-8, which no source can carry. Rename it to a path that's UTF-8."
        )
    if subst := marked(copied):
        raise Unbuildable(
            f"{subst[0].text} has git's export-subst attribute set, so a tarball of the repo would expand its "
            f"placeholders and a clone wouldn't. Remove export-subst for {subst[0].text} from .gitattributes."
        )
    rewrites: dict[str, tuple[Callable[[str, Blueprint, Selection], str], str]] = {
        in_python_root(catalog, "pyproject.toml"): (pyproject.rewrite, "deps"),
        in_python_root(catalog, ".env.example"): (envfile.process, "env"),
    }
    from_overlay = {change.path: source for source, change in overlaid}
    for index, change in enumerate(found):
        rewrite = rewrites.get(change.path)
        source = change.source or change.path
        if rewrite is None or (change.kind == "copy" and breaks_the_rule(root, source) is not None):
            continue
        try:
            given = change.text if change.kind == "write" else (root / source).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise Unbuildable(f"{source} isn't UTF-8 text. Save it as UTF-8.") from None
        try:
            text = rewrite[0](given, catalog, selection)
        except (pyproject.PyprojectError, envfile.EnvFileError) as error:
            named = f"{from_overlay[change.path]}: " if change.path in from_overlay else ""
            raise Unbuildable(f"{named}{error}") from None
        found[index] = Change("write", change.path, text)
    origins = {op.path: op.origin for op in made.ops if op.op in ("copy", "render") and op.path != python_root_lock(catalog)}
    origins.update({change.path: OVERLAY for _, change in overlaid})
    origins.update({path: origin for path, (_, origin) in rewrites.items()})
    return Assembled(found, origins)
