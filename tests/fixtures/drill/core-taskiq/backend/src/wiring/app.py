"""The composition root: what the app mounts, starts and installs."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI

from ..infrastructure.composition import Lifecycle, RouterMount
from ..infrastructure.taskiq.lifecycle import lifecycle as taskiq_lifecycle

ROUTER_MOUNTS: tuple[RouterMount, ...] = ()
ROOT_ROUTERS: tuple[APIRouter, ...] = ()
API_THROTTLE: tuple[Any, ...] = ()
LIFECYCLES: tuple[Lifecycle, ...] = (
    taskiq_lifecycle,
)
INSTALLERS: tuple[Callable[[FastAPI], None], ...] = ()
DOCS_GUARD: Callable[..., Any] | None = None
