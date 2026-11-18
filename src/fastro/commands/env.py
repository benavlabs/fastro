"""``fastro env``: the built-in ``env secret`` and ``env validate``, for where the blueprint declares neither (spec 2.9).

- ``fastro env secret`` prints ``secrets.token_hex``.
- ``fastro env validate`` refuses, since only a blueprint's own validator can audit a project.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

import typer

from ..runner import TOKEN_BYTES, TOKEN_DEFAULT

app = typer.Typer(no_args_is_help=True, help="Inspect and prepare the runtime environment.")


@app.command("secret")
def secret(
    bytes_: int = typer.Option(
        TOKEN_DEFAULT,
        "--bytes",
        min=TOKEN_BYTES.start,
        max=TOKEN_BYTES.stop - 1,
        help="Number of random bytes (hex output is 2x).",
    ),
) -> None:
    """Generate a high-entropy hex secret suitable for ``SECRET_KEY``."""
    typer.echo(secrets.token_hex(bytes_))


OUTSIDE = "fastro env validate runs in a project. Run it from inside one."


def validate_command(reason: str) -> Callable[[], None]:
    """The built-in ``env validate``, which exits 2 saying ``reason``: there's no project, or no validator to run."""

    def validate() -> None:
        """Audit this environment the way production would, with the blueprint's own validator."""
        typer.echo(f"error: {reason}", err=True)
        raise typer.Exit(2)

    return validate


app.command("validate")(validate_command(OUTSIDE))
