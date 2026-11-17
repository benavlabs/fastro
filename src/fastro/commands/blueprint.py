"""``fastro blueprint``: the commands a blueprint repo's maintainers run (spec 2.13)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .. import lint as linting
from .. import verify as verifying
from ..apply import ApplyError, RepositoryError, present, unreadable_config
from ..engine import blueprint, catalog_check
from ..engine.blueprint import IncompatibleCatalog
from ..engine.problems import CatalogError
from ..tools import MissingToolError, NotInstalled, installed_version, require

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


@app.command("verify")
def verify(
    sets: list[str] = typer.Argument(None, metavar="[SET]...", help="The verify sets to build; every set without one."),
    keep: bool = typer.Option(False, "--keep", help="Leave the scratch copies on disk."),
) -> None:
    """Build each verify set through the engine, and check the project it gives."""
    root = Path.cwd()
    file = root / "blueprint.toml"
    try:
        there, refused = present(file), unreadable_config(root, "blueprint.toml")
    except OSError as error:
        typer.echo(
            f"error: can't read {file}: {error.strerror}. Check its permissions, then run fastro blueprint verify again.",
            err=True,
        )
        raise typer.Exit(3) from None
    if not there:
        typer.echo(f"error: {root} has no blueprint.toml. Run fastro blueprint verify in a blueprint repo.", err=True)
        raise typer.Exit(2)
    if refused is not None:
        typer.echo(f"error: {refused}", err=True)
        raise typer.Exit(2)
    try:
        catalog = blueprint.load("blueprint.toml", catalog_check.read_text(file), installed_version())
        verifying.unknown(catalog, sets or [])
        require("uv")
        code = verifying.verify(root, catalog, sets or [], verifying.checks_for(catalog), keep)
    except CatalogError as error:
        for problem in error.problems:
            typer.echo(str(problem), err=True)
        typer.echo("Fix these first: fastro blueprint lint lists them all.", err=True)
        raise typer.Exit(3 if isinstance(error, IncompatibleCatalog) else 1) from None
    except (verifying.Unbuildable, ApplyError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from None
    except (verifying.UnknownSet, RepositoryError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from None
    except MissingToolError as missing:
        typer.echo(f"error: fastro blueprint verify builds and checks through {missing.tool}, which isn't on PATH.", err=True)
        raise typer.Exit(3) from None
    except OSError as error:
        where = f"{error.filename}: " if error.filename else ""
        typer.echo(f"error: {where}{error.strerror or error}. Fix that, then run fastro blueprint verify again.", err=True)
        raise typer.Exit(3) from None
    except NotInstalled as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(3) from None
    raise typer.Exit(code)
