"""The project's settings: each selected feature's mixin on top of the core."""

from ..infrastructure.auth.settings import AccountsSettings
from ..infrastructure.config.base import CoreSettings


class Settings(
    AccountsSettings,
    CoreSettings,
):
    """The settings of every selected feature, on top of the core."""


settings = Settings()
