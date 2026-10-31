"""The composition root: what the app mounts, starts and installs."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI

from ..infrastructure.composition import Lifecycle, RouterMount

ROUTER_MOUNTS: tuple[RouterMount, ...] = ()
ROOT_ROUTERS: tuple[APIRouter, ...] = ()
API_THROTTLE: tuple[Any, ...] = ()
LIFECYCLES: tuple[Lifecycle, ...] = ()
INSTALLERS: tuple[Callable[[FastAPI], None], ...] = ()
DOCS_GUARD: Callable[..., Any] | None = None
