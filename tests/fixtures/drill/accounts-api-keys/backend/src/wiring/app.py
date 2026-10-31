"""The composition root: what the app mounts, starts and installs."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI

from ..infrastructure.composition import Lifecycle, RouterMount
from ..infrastructure.auth.dependencies import get_current_superuser
from ..infrastructure.auth.install import install as accounts_install
from ..infrastructure.auth.routes import root_routers as accounts_root_routers
from ..infrastructure.auth.routes import router as auth_router
from ..infrastructure.auth.setup import lifecycle as accounts_lifecycle
from ..modules.user.routes import router as users_router
from ..modules.api_keys.install import install as api_keys_install
from ..modules.api_keys.routes import router as api_keys_router
from ..infrastructure.handover import lifecycle as handover_lifecycle

ROUTER_MOUNTS: tuple[RouterMount, ...] = (
    RouterMount(users_router, "/users", throttled=True),
    RouterMount(auth_router, "/auth", throttled=False),
    RouterMount(api_keys_router, "/api-keys", throttled=True),
)
ROOT_ROUTERS: tuple[APIRouter, ...] = accounts_root_routers
API_THROTTLE: tuple[Any, ...] = ()
LIFECYCLES: tuple[Lifecycle, ...] = (
    accounts_lifecycle,
    handover_lifecycle,
)
INSTALLERS: tuple[Callable[[FastAPI], None], ...] = (
    accounts_install,
    api_keys_install,
)
DOCS_GUARD: Callable[..., Any] | None = get_current_superuser
