"""A router mounted at a prefix."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mount:
    """A router and the prefix it's mounted at."""

    router: object
    prefix: str
