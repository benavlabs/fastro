"""A blueprint's overlay (spec 2.1): the files under ``[blueprint].overlay`` reach a project at their path under it,
each ``*.j2`` file rendered with the template context (6.2) and its suffix dropped."""

from __future__ import annotations

from collections.abc import Container, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from fastro import templates
from fastro.apply import Change
from fastro.engine.blueprint import Blueprint
from fastro.engine.files import breaks_the_rule, file_kind
from fastro.engine.spec import RepoPath

TEMPLATE = ".j2"
ENV = ".env"
NOTHING = "so nothing was written."
OWN = ("fastro.toml", ".fastro")


class OverlayError(Exception):
    """An overlay a project can't be given: a template that can't be read or rendered, or a project path two
    sources would both give."""


def project_path(catalog: Blueprint, name: str) -> str | None:
    """The project path the overlay gives the file ``name``: its path under the overlay minus a ``.j2`` suffix, or
    ``None`` when the catalog has no overlay or ``name`` isn't in it (2.1)."""
    if catalog.info.overlay is None:
        return None
    base = catalog.info.overlay.text.rstrip("/") + "/"
    return name[len(base) :].removesuffix(TEMPLATE) if name.startswith(base) else None


def fastro_own(path: str) -> bool:
    """Whether ``path`` is one fastro writes in a project itself: ``fastro.toml``, ``.fastro``, or a path under
    either (2.1)."""
    return path.split("/", 1)[0] in OWN


def env_files(catalog: Blueprint, names: Iterable[RepoPath], submodules: Container[str], received: bool) -> list[str]:
    """Each of ``names`` that's a ``.env`` (2.1): a file named exactly ``.env``, at any depth, or an overlay file
    whose project path is ``.env`` or ends in ``/.env``, never one of ``submodules``, which git records as a
    submodule rather than a file. With ``received``, only those a project would receive: in the overlay, or outside
    ``blueprint_only``."""
    found = []
    for path in names:
        if path.text in submodules:
            continue
        given = project_path(catalog, path.text)
        if given is not None:
            if given.rsplit("/", 1)[-1] == ENV:
                found.append(path.text)
        elif path.text.rsplit("/", 1)[-1] == ENV:
            if not received or not any(only.covers(path.text) for only in catalog.info.all_blueprint_only):
                found.append(path.text)
    return found


def files(
    root: Path, catalog: Blueprint, contributed: Iterable[RepoPath], values: Mapping[str, Any]
) -> list[tuple[str, Change]]:
    """Each overlay file among ``contributed``, with the change that gives a project its file: a copy, or for a
    ``*.j2`` file the text it renders to with ``values`` (``templates.render``), both at its project path. Nothing
    when the catalog names no overlay.

    Raises:
        OverlayError: a template is named only ``.j2``, an overlay file's project path is one of fastro's own
            (``fastro_own``), two overlay files share a project path or one's is a directory another's sits under, or
            a template is a symlink, breaks spec 1.10's rule, isn't UTF-8 past a
            byte order mark or doesn't render.
        OSError: the system won't let fastro read a template.
    """
    if catalog.info.overlay is None:
        return []
    base = catalog.info.overlay.text.rstrip("/") + "/"
    found: dict[str, str] = {}
    for name in sorted(path.text for path in contributed if path.text.startswith(base)):
        if name.rsplit("/", 1)[-1] == TEMPLATE:
            raise OverlayError(f"{name} is named only {TEMPLATE}, so it gives a project no path, {NOTHING}")
        path = name[len(base) :].removesuffix(TEMPLATE)
        if fastro_own(path):
            raise OverlayError(
                f"{name} would give a project {path}, and fastro.toml, .fastro and what's under them are paths fastro "
                f"writes itself, {NOTHING}"
            )
        if path in found:
            raise OverlayError(f"{found[path]} and {name} would both give a project {path}, {NOTHING}")
        found[path] = name
    for path, name in found.items():
        for above in parents(path):
            if above in found:
                raise OverlayError(f"{name} and {found[above]} {_both(above)}")
    given: list[tuple[str, Change]] = []
    for path, name in found.items():
        if not name.endswith(TEMPLATE):
            given.append((name, Change("copy", path, source=name)))
            continue
        if file_kind(root / name, follow=False) == "symlink":
            raise OverlayError(f"{name} is a symlink, and a project gets none from its source (1.10), {NOTHING}")
        if (reason := breaks_the_rule(root, name)) is not None:
            raise OverlayError(f"{name} {reason}. fastro reads an overlay template only as a regular file inside the repo.")
        try:
            given.append((name, Change("write", path, templates.render(name, (root / name).read_bytes(), values))))
        except templates.TemplateError as error:
            raise OverlayError(str(error)) from None
    return given


def check(
    planned: Sequence[Change], overlay: Sequence[tuple[str, Change]], wiring: Iterable[str], origins: Mapping[str, str]
) -> None:
    """Refuse a plan that would give one path both an overlay file and another source (7), or a path that's a file
    from one source and a directory from another, overlay or not (2.1). Each message names both paths, a wiring file
    as one and any other planned file with its ``origins`` entry.

    Raises:
        OverlayError: an overlay file's project path is one ``planned`` copies or writes, or a directory one of its
            paths sits under, or sits under one of them; or one planned path sits under another.
    """
    rendered = set(wiring)
    others = {change.path: change for change in planned}
    under: dict[str, Change] = {}
    for change in planned:
        for above in parents(change.path):
            under.setdefault(above, change)

    def source(change: Change) -> str:
        if change.path in rendered:
            return f"the wiring file {change.path}"
        named = change.source or change.path
        return f"{named} ({origins[change.path]})" if change.path in origins else named

    for name, change in overlay:
        if (other := others.get(change.path)) is not None:
            raise OverlayError(f"{name} and {source(other)} would both give a project {change.path}, {NOTHING}")
        if (other := under.get(change.path)) is not None:
            raise OverlayError(f"{source(other)} and {name} {_both(change.path)}")
        for above in parents(change.path):
            if (other := others.get(above)) is not None:
                raise OverlayError(f"{name} and {source(other)} {_both(above)}")
    for change in sorted(planned, key=lambda change: change.path):
        for above in parents(change.path):
            if (other := others.get(above)) is not None:
                raise OverlayError(f"{source(change)} and {source(other)} {_both(above)}")


def parents(path: str) -> list[str]:
    """The directories ``path`` sits under, outermost first."""
    parts = path.split("/")
    return ["/".join(parts[:count]) for count in range(1, len(parts))]


def _both(path: str) -> str:
    return f"would give a project both a file {path} and files under it, {NOTHING}"
