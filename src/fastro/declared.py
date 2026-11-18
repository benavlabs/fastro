"""The commands the blueprint declares, for the repo fastro finds ``fastro.toml`` in (spec 2.9)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from fastro.apply import present, unreadable_config
from fastro.engine import blueprint, project
from fastro.engine.blueprint import Command
from fastro.engine.catalog_check import read_text
from fastro.engine.problems import CatalogError
from fastro.engine.resolve import SelectionError, select
from fastro.engine.spec import RepoPath
from fastro.runner import available
from fastro.tools import NotInstalled, installed_version


class Unavailable(Exception):
    """The repo has a ``fastro.toml``, and its declared commands can't be added: the message says why."""


@dataclass(frozen=True)
class Declared:
    """The repo's root, its ``python_root``, and the commands its selection has, by their words."""

    root: Path
    python_root: RepoPath
    commands: Mapping[tuple[str, ...], Command]


def find(start: Path) -> Path | None:
    """The nearest directory from ``start`` upward that holds a ``fastro.toml``, or ``None``.

    Raises:
        Unavailable: the system won't let fastro look in one of them.
    """
    for candidate in (start, *start.parents):
        try:
            if present(candidate / "fastro.toml"):
                return candidate
        except OSError as error:
            raise Unavailable(
                f"can't look for fastro.toml in {candidate}: {error.strerror}, so no declared commands are available."
            ) from None
    return None


def load(root: Path) -> Declared:
    """The declared commands of the blueprint repo at ``root``, read as ``sync`` reads its two files (spec 3).

    Raises:
        Unavailable: ``root`` is a project, its files can't be read or have problems, or fastro isn't installed.
    """
    try:
        text = _config(root, "fastro.toml")
        if not project.load("fastro.toml", text).source:
            raise Unavailable(
                "fastro.toml doesn't set source = true, so this is a project, and declared commands in a project come "
                "with fastro new."
            )
        if not present(root / "blueprint.toml"):
            raise Unavailable("fastro.toml sets source = true, but there's no blueprint.toml beside it.")
        catalog = blueprint.load("blueprint.toml", _config(root, "blueprint.toml"), installed_version())
        loaded = project.load("fastro.toml", text, catalog)
        selection = select(catalog, loaded.features, loaded.app.requires, loaded.options)
    except OSError as error:
        raise Unavailable(
            f"can't read {error.filename}: {error.strerror}, so the declared commands aren't available."
        ) from None
    except CatalogError as error:
        raise Unavailable(
            f"{error.problems[0].file} has problems, so this repo's declared commands aren't available. "
            "fastro blueprint lint lists them."
        ) from None
    except SelectionError as error:
        raise Unavailable(f"{error} So this repo's declared commands aren't available.") from None
    except NotInstalled as error:
        raise Unavailable(str(error)) from None
    return Declared(root, catalog.info.python_root, available(catalog, selection))


def _config(root: Path, name: str) -> str:
    refused = unreadable_config(root, name)
    if refused is not None:
        raise Unavailable(refused)
    return read_text(root / name)
