"""The project's settings: each selected feature's mixin on top of the core."""

from ..infrastructure.auth.settings import AccountsSettings
from ..infrastructure.cache.settings import CacheSettings
from ..infrastructure.config.base import CoreSettings
from ..infrastructure.ratelimit.settings import RateLimitSettings
from ..infrastructure.taskiq.settings import TaskiqSettings
from ..interfaces.admin.settings import SQLAdminSettings
from ..modules.api_keys.settings import APIKeySettings
from ..modules.tier.settings import TierSettings


class Settings(
    AccountsSettings,
    RateLimitSettings,
    TierSettings,
    APIKeySettings,
    SQLAdminSettings,
    CacheSettings,
    TaskiqSettings,
    CoreSettings,
):
    """The settings of every selected feature, on top of the core."""


settings = Settings()
