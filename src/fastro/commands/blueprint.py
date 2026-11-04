"""``fastro blueprint``: the commands a blueprint repo's maintainers run (spec 2.13)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .. import lint as linting
from ..engine.blueprint import IncompatibleCatalog
from ..tools import MissingToolError, NotInstalled, installed_version

app = typer.Typer(no_args_is_help=True, help="Check and verify a blueprint repo.")


@app.command("lint")
def lint(json_: bool = typer.Option(False, "--json", help="Print spec 7's lint document.")) -> None:
    """Check blueprint.toml and fastro.toml in this blueprint repo. It reads files only."""
    root = Path.cwd()
    try:
        report = linting.lint(root)
        version = str(installed_version())
    except linting.UsageError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from None
    except (linting.Unreadable, NotInstalled) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(3) from None
    except MissingToolError as missing:
        typer.echo(
            f"error: fastro blueprint lint lists the repo's tracked files with {missing.tool}, which isn't on PATH.", err=True
        )
        raise typer.Exit(3) from None
    except IncompatibleCatalog as error:
        for problem in error.problems:
            typer.echo(str(problem), err=True)
        raise typer.Exit(3) from None
    if json_:
        typer.echo(json.dumps(report.document(version), indent=2))
    else:
        for line in linting.human(report, root):
            typer.echo(line)
    raise typer.Exit(1 if report.findings else 0)
