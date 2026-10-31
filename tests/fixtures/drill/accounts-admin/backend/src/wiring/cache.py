"""The decorator a route caches its response with."""

from ..infrastructure.uncached import cached

__all__ = ["cached"]
