"""The project's settings: each selected feature's mixin on top of the core."""

from ..infrastructure.auth.settings import AccountsSettings
from ..infrastructure.cache.settings import CacheSettings
from ..infrastructure.config.base import CoreSettings
from ..infrastructure.taskiq.settings import TaskiqSettings


class Settings(
    AccountsSettings,
    CacheSettings,
    TaskiqSettings,
    CoreSettings,
):
    """The settings of every selected feature, on top of the core."""


settings = Settings()
