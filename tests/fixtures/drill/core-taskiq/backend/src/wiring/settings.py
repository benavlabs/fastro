"""The project's settings: each selected feature's mixin on top of the core."""

from ..infrastructure.config.base import CoreSettings
from ..infrastructure.taskiq.settings import TaskiqSettings


class Settings(
    TaskiqSettings,
    CoreSettings,
):
    """The settings of every selected feature, on top of the core."""


settings = Settings()
