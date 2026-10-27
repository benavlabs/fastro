"""``fastro make``: generate files a project owns from then on.

``fastro make deploy`` renders the compose files from ``fastro._legacy_deploy``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import typer

from .. import _legacy_deploy
from .._legacy_deploy import DEFAULT_INTERNAL_SUBNET
from ..lib.project import discover_project
from ..lib.prompts import error, info

app = typer.Typer(no_args_is_help=True, help="Generate files the project owns from then on.")


class DeployMode(StrEnum):
    local = "local"
    prod = "prod"
    nginx = "nginx"


@app.command("deploy")
def deploy(
    mode: DeployMode = typer.Argument(
        ...,
        help="Deployment mode to generate. Pick `local` for hot-reload dev, `prod` for "
        "single-host production, `nginx` for production behind a reverse proxy.",
    ),
    output_dir: Path = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Where to write the compose file. Defaults to the repo root.",
    ),
    api_port: int = typer.Option(8000, "--api-port", help="Host port to publish the API on."),
    workers: int = typer.Option(4, "--workers", help="Number of API workers (prod / nginx only)."),
    internal_subnet: str = typer.Option(
        DEFAULT_INTERNAL_SUBNET,
        "--internal-subnet",
        help="Subnet for the compose network, and the only peers uvicorn takes forwarded headers from (nginx only).",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files without asking."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Assume yes for all prompts."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be written, don't touch disk."),
) -> None:
    """Generate ``docker-compose.yml`` (and ``nginx/default.conf`` for nginx mode)."""
    project = discover_project(output_dir)

    target_root = (output_dir or project.repo_root).resolve()
    if not dry_run:
        target_root.mkdir(parents=True, exist_ok=True)

    subnet = internal_subnet if mode == DeployMode.nginx else DEFAULT_INTERNAL_SUBNET
    try:
        plan = _legacy_deploy.plan(mode.value, project, target_root, api_port=api_port, workers=workers, internal_subnet=subnet)
    except ValueError as failure:
        error(f"--internal-subnet: {failure}")
        raise typer.Exit(code=1) from failure

    info(f"deploy: generating '{mode.value}' compose for {project.repo_root}")
    result = _legacy_deploy.write(plan, dry_run=dry_run, assume_yes=force or yes)

    if result.skipped:
        info("")
        info(f"{len(result.skipped)} file(s) skipped.")
    if dry_run:
        info("")
        info("dry-run complete: no files were written.")
        return

    info("")
    info("done. Next steps:")
    if mode == DeployMode.local:
        info("  docker compose up --build")
        return

    info("  cp backend/.env.example backend/.env  # if you haven't already")
    info("  # with migrations/versions empty, generate and commit the baseline first,")
    info("  # against a database with nothing in it, or upgrade writes an empty revision:")
    info("  #   docs/user-guide/database/migrations.md#alembic-migrations")
    info("  docker compose build")
    info("  docker compose --profile migrate run --rm migrate")
    info("  docker compose up -d")
    if mode == DeployMode.nginx:
        info("  curl -i http://localhost/health/ready")
