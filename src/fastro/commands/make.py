"""``fastro make``: render one of the blueprint's generators into the repo (spec 2.9)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

import typer

from .. import make as making
from .. import sync as syncing
from ..apply import RepositoryError, file_kind, tracked_files
from ..engine.blueprint import ExtendingCatalogError, IncompatibleCatalog
from ..engine.problems import CatalogError
from ..engine.resolve import SelectionError, select
from ..tools import MissingToolError, NotInstalled, installed_version

EXTENDS = "blueprint.toml extends {name}, and this fastro can't merge an extending catalog with its base yet."


def make(context: typer.Context) -> None:
    """Render one of the blueprint's generators, such as the deploy compose files, into the repo."""
    args = list(context.args)
    root = Path.cwd()
    try:
        source = syncing.load_source(root, "make")
    except syncing.UsageError as error:
        if making.wants_help(args):
            _lines(making.usage(None))
            raise typer.Exit(0) from None
        _fail(str(error), 2)
    except (syncing.Unreadable, NotInstalled) as error:
        _fail(str(error), 3)
    except ExtendingCatalogError:
        _fail(EXTENDS.format(name="another catalog"), 3)
    except IncompatibleCatalog as error:
        _problems(error, 3)
    except CatalogError as error:
        _problems(error, 1)
    if source.catalog.info.extends is not None:
        _fail(EXTENDS.format(name=source.catalog.info.extends.name), 3)
    if not args or args[0] == making.HELP:
        _lines(making.usage(source.catalog))
        raise typer.Exit(0)
    try:
        request = making.parse(source.catalog, args)
        if request.help:
            _lines(making.usage(source.catalog, request))
            raise typer.Exit(0)
        selection = select(source.catalog, source.project.features, source.project.app.requires, source.project.options)
        values = making.context(source, selection, request.values, str(installed_version()))
        files = making.render(root, request, (path.text for path in tracked_files(root)), values)
        output = root / request.output if request.output is not None else root
        making.check(output, files)
    except making.UsageError as error:
        _fail(str(error), 2)
    except RepositoryError as error:
        _fail(f"{error}. fastro make renders the files git tracks, so run it in a blueprint repo.", 2)
    except (making.Refused, SelectionError) as error:
        _fail(str(error), 1)
    except MissingToolError as missing:
        _fail(f"fastro make reads the repo's tracked files with {missing.tool}, which isn't on PATH.", 3)
    except (OSError, NotInstalled) as error:
        _fail(_unreadable(error), 3)
    shown = Path(request.output) if request.output is not None else Path()
    try:
        _write(output, shown, files, request)
    except OSError as error:
        _fail(_unreadable(error), 3)


def _write(output: Path, shown: Path, files: list[making.File], request: making.Request) -> None:
    for file in files:
        target, name = output / file.path, shown / file.path
        current = target.read_bytes() if file_kind(target) == "file" else None
        if current == file.data:
            typer.echo(f"unchanged {name}")
            continue
        if request.dry_run:
            replacing = " (replacing what is there)" if current is not None else ""
            typer.echo(f"would write {name} ({len(file.data)} bytes){replacing}")
            continue
        if current is not None and not request.force:
            if not _terminal():
                typer.echo(f"kept {name}: it differs, and there's no terminal to ask on. Run again with --force to replace it.")
                continue
            if not typer.confirm(f"{name} differs from what fastro make would write. Replace it?", default=False):
                typer.echo(f"kept {name}")
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.data)
        typer.echo(f"wrote {name}")


def _terminal() -> bool:
    return sys.stdin.isatty()


def _unreadable(error: OSError | NotInstalled) -> str:
    if isinstance(error, NotInstalled):
        return str(error)
    where = f"{error.filename}: " if error.filename else ""
    return f"{where}{error.strerror or error}. Fix that, then run fastro make again."


def _lines(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)


def _problems(error: CatalogError, code: int) -> NoReturn:
    for problem in error.problems:
        typer.echo(str(problem), err=True)
    raise typer.Exit(code)


def _fail(message: str, code: int) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)
