"""``fastro env``: inspect and prepare the runtime environment.

Both commands run the project's own scripts in its interpreter:

- ``fastro env secret`` runs ``scripts.gen_secret``, which prints a key the
  project's rules accept. Outside a project it prints ``secrets.token_hex``.

- ``fastro env validate`` runs ``scripts.validate_env``, the production security
  validator, regardless of the configured environment.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import typer

from .. import runner
from ..lib.project import discover_project
from ..lib.prompts import error

VALIDATE_MODULE = "scripts.validate_env"
GEN_SECRET_MODULE = "scripts.gen_secret"
UV_INSTALL_URL = "https://docs.astral.sh/uv/getting-started/installation/"

app = typer.Typer(no_args_is_help=True, help="Inspect and prepare the runtime environment.")


def _project_backend() -> Path | None:
    """The ``backend/`` of the project this command was run in, if there is one."""
    backend = discover_project().backend_dir
    return backend if (backend / "pyproject.toml").is_file() else None


def _run_script(command: str, backend: Path, module: str, args: list[str]) -> None:
    """Run ``module`` in the project's interpreter, and exit with its code."""
    script = runner.module_file(backend, module)
    if not script.is_file():
        shown = script.relative_to(backend.parent)
        error(f"`fastro env {command}` is not available in this project: {shown} doesn't exist.")
        raise typer.Exit(code=3)

    try:
        code = runner.run_module(backend, module, args)
    except runner.MissingToolError as missing:
        error(
            f"`fastro env {command}` runs in the project through {missing.tool}, which isn't on PATH. "
            f"Install it: {UV_INSTALL_URL}"
        )
        raise typer.Exit(code=3) from missing

    raise typer.Exit(code=code)


@app.command("secret")
def secret(
    bytes_: int = typer.Option(32, "--bytes", min=16, max=128, help="Number of random bytes (hex output is 2x)."),
) -> None:
    """Generate a high-entropy hex secret suitable for ``SECRET_KEY``."""
    backend = _project_backend()
    if backend is None:
        typer.echo(secrets.token_hex(bytes_))
        return

    _run_script("secret", backend, GEN_SECRET_MODULE, ["--bytes", str(bytes_)])


@app.command("validate")
def validate() -> None:
    """Run the production security validator against the current settings."""
    backend = _project_backend()
    if backend is None:
        error("No `backend/pyproject.toml` in this directory or above it: run this from inside a project.")
        raise typer.Exit(code=1)

    _run_script("validate", backend, VALIDATE_MODULE, [])
