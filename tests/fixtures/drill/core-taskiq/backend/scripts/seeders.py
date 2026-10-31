"""The initial-data steps ``scripts/setup_initial_data.py`` runs, in order."""

from collections.abc import Awaitable, Callable

SEEDERS: tuple[Callable[[], Awaitable[None]], ...] = ()
