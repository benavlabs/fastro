"""The initial-data steps ``scripts/setup_initial_data.py`` runs, in order."""

from collections.abc import Awaitable, Callable
from scripts.create_first_superuser import create_first_superuser

SEEDERS: tuple[Callable[[], Awaitable[None]], ...] = (
    create_first_superuser,
)
