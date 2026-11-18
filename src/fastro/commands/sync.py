"""``fastro sync``: make the generated files match ``fastro.toml`` (spec 3; rev 1.30)."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import sync as syncing
from ..apply import ApplyError, apply
from ..apply import check as allowed
from ..engine.blueprint import IncompatibleCatalog
from ..engine.problems import CatalogError
from ..engine.render import RenderError
from ..engine.resolve import SelectionError
from ..tools import NotInstalled


def sync(
    check: bool = typer.Option(False, "--check", help="Write nothing; print a diff of each file that differs, and exit 1."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Write nothing; print each path sync would write or delete."),
) -> None:
    """Make the generated files match fastro.toml. In a blueprint repo, that's the wiring."""
    if check and dry_run:
        typer.echo("error: --check and --dry-run can't be used together.", err=True)
        raise typer.Exit(2)
    try:
        source = syncing.load_source(Path.cwd())
        found = syncing.wiring_changes(source)
    except syncing.UsageError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from None
    except (syncing.Unreadable, NotInstalled) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(3) from None
    except (CatalogError, SelectionError, RenderError) as error:
        problems = error.problems if isinstance(error, CatalogError) else [error]
        for problem in problems:
            typer.echo(str(problem), err=True)
        raise typer.Exit(3 if isinstance(error, IncompatibleCatalog) else 1) from None
    if check or dry_run:
        try:
            allowed(found, source.root, source.root, only=syncing.wiring_paths(source))
        except ApplyError as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(1) from None
    if check:
        try:
            diffs = [syncing.diff(source.root, change) for change in found]
        except syncing.Unreadable as error:
            typer.echo(f"error: {error}", err=True)
            raise typer.Exit(3) from None
        for text in diffs:
            typer.echo(text, nl=False)
        raise typer.Exit(1 if found else 0)
    if dry_run:
        for change in found:
            typer.echo(change.path)
        return
    try:
        apply(found, source.root, source.root, only=syncing.wiring_paths(source))
    except ApplyError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from None
    for change in found:
        typer.echo(f"{'deleted' if change.kind == 'delete' else 'wrote'} {change.path}")
