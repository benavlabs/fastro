"""The compose files ``fastro make deploy`` writes, from the templates the boilerplate's ``bp`` shipped.

Three modes are supported:

- ``local``: hot-reload dev stack (mounts source, exposes port 8000)
- ``prod``: production stack with multiple workers, ports exposed directly
- ``nginx``: ``prod`` plus an nginx reverse proxy on port 80

All modes target the existing multi-stage ``backend/Dockerfile``;
no per-mode Dockerfile is generated. Modes only differ in the
compose file (and an optional nginx config).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path
from typing import Any

from ..lib.project import ProjectContext
from ..lib.prompts import confirm_overwrite, info, success, warn
from ..lib.render import Renderer

DEFAULT_INTERNAL_SUBNET = "172.31.240.0/24"
SMALLEST_TRUSTED_PREFIX = {4: 8, 6: 48}
TASKIQ_PACKAGE = "src/infrastructure/taskiq"
"""The feature whose worker and scheduler services the compose files carry."""

TEMPLATES = Path(__file__).parent / "templates"


def validated_subnet(value: str) -> str:
    """The subnet in the form ``ipaddress`` reads, refusing one that trusts too much.

    Takes a network of ``/8`` or smaller for IPv4, ``/48`` or smaller for IPv6.

    Raises:
        ValueError: ``value`` is not a network address, carries host bits, or covers
            more than the smallest prefix its family allows.
    """
    try:
        network = ip_network(value, strict=True)
    except ValueError as error:
        raise ValueError(f"{value!r} is not a network address, such as 172.31.240.0/24.") from error

    smallest = SMALLEST_TRUSTED_PREFIX[network.version]
    if network.prefixlen < smallest:
        raise ValueError(f"{value!r} covers {network.num_addresses} addresses; name a range of /{smallest} or smaller.")

    return str(network)


@dataclass(frozen=True)
class Render:
    """A template, relative to ``TEMPLATES``, and the file it's written to."""

    template: str
    target: Path


@dataclass(frozen=True)
class Plan:
    """The files a mode writes, and the context its templates render with."""

    context: dict[str, Any]
    files: tuple[Render, ...]


@dataclass
class WriteResult:
    """What a write actually did."""

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)


def plan(
    mode: str,
    project: ProjectContext,
    target_root: Path,
    *,
    api_port: int,
    workers: int,
    internal_subnet: str = DEFAULT_INTERNAL_SUBNET,
) -> Plan:
    """The compose file (and nginx config) ``mode`` writes under ``target_root``.

    Raises:
        ValueError: ``internal_subnet`` is refused by :func:`validated_subnet`.
    """
    context = {
        "mode": mode,
        "project_name": project.repo_root.name,
        "api_port": api_port,
        "workers": workers,
        "postgres_image": "postgres:16-alpine",
        "redis_image": "redis:7-alpine",
        "nginx_image": "nginx:1.27-alpine",
        "backend_context": "./backend",
        "build_context": ".",
        "backend_dockerfile": "backend/Dockerfile",
        "env_file": "./backend/.env",
        "internal_subnet": validated_subnet(internal_subnet),
        "taskiq": (project.backend_dir / TASKIQ_PACKAGE).is_dir(),
    }

    files = [Render(f"{mode}/docker-compose.yml.j2", target_root / "docker-compose.yml")]
    if mode == "nginx":
        files.append(Render("nginx/default.conf.j2", target_root / "nginx" / "default.conf"))

    return Plan(context=context, files=tuple(files))


def write(plan: Plan, *, dry_run: bool, assume_yes: bool) -> WriteResult:
    """Render each file in ``plan``, asking before replacing one unless ``assume_yes``.

    A dry run renders and reports each file with its size in bytes, and writes nothing.
    """
    result = WriteResult()
    renderer = Renderer(TEMPLATES)

    for render in plan.files:
        if render.target.exists() and not dry_run and not confirm_overwrite(str(render.target), assume_yes=assume_yes):
            warn(f"skip {render.target} (kept existing)")
            result.skipped.append(render.target)
            continue

        content = renderer.render(render.template, plan.context)

        if dry_run:
            replacing = " (replacing what is there)" if render.target.exists() else ""
            info(f"would write {render.target} ({len(content.encode('utf-8'))} bytes){replacing}")
            result.written.append(render.target)
            continue

        render.target.parent.mkdir(parents=True, exist_ok=True)
        render.target.write_text(content, encoding="utf-8")
        success(f"wrote {render.target}")
        result.written.append(render.target)

    return result
