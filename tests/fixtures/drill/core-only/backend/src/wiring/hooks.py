"""The contributions features make to each other's extension points."""

from ..infrastructure.composition import (
    PermissionSource,
    RateLimitResolver,
    ReadinessCheck,
    TierDeleteGuard,
    TierDeleteRelease,
)
from ..infrastructure.database.health import readiness as database_readiness
from ..infrastructure.database.health import schema_readiness

PERMISSION_SOURCES: tuple[PermissionSource, ...] = ()
RATE_LIMIT_RESOLVERS: tuple[RateLimitResolver, ...] = ()
TIER_DELETE_GUARDS: tuple[TierDeleteGuard, ...] = ()
TIER_DELETE_RELEASES: tuple[TierDeleteRelease, ...] = ()
CRITICAL_READINESS_CHECKS: tuple[ReadinessCheck, ...] = (database_readiness, schema_readiness,)
INFORMATIONAL_READINESS_CHECKS: tuple[ReadinessCheck, ...] = ()
