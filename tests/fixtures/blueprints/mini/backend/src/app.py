"""The mini app, put together from its wiring."""

from .wiring import admin, app, health, tasks


def prefixes() -> list[str]:
    """The prefix of every mounted router, in mount order."""
    return [mount.prefix for mount in app.ROUTERS]


def ready() -> bool:
    """Whether every readiness check passes."""
    return all(check() for check in health.HEALTH_CHECKS)


def site() -> str:
    """The admin site the app mounts."""
    return admin.SITE


def task_modules() -> list[str]:
    """The modules that hold background tasks."""
    return tasks.TASK_MODULES
