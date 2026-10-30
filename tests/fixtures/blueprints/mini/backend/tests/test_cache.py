"""The app's settings take the cache's."""

from src.wiring.app import Settings


def test_settings_include_the_cache_backend() -> None:
    assert Settings.cache_backend == "memory"
