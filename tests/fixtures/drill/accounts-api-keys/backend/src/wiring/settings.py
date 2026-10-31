"""The project's settings: each selected feature's mixin on top of the core."""

from ..infrastructure.auth.settings import AccountsSettings
from ..infrastructure.config.base import CoreSettings
from ..modules.api_keys.settings import APIKeySettings


class Settings(
    AccountsSettings,
    APIKeySettings,
    CoreSettings,
):
    """The settings of every selected feature, on top of the core."""


settings = Settings()
