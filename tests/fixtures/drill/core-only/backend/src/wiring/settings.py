"""The project's settings: each selected feature's mixin on top of the core."""

from ..infrastructure.config.base import CoreSettings


class Settings(
    CoreSettings,
):
    """The settings of every selected feature, on top of the core."""


settings = Settings()
