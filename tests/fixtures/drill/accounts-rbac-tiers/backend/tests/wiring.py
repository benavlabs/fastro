"""Fixture modules of the selected features, loaded by ``tests/conftest.py``."""

PYTEST_PLUGINS: list[str] = [
    "tests.fixtures.accounts",
    "tests.fixtures.tiers",
]
