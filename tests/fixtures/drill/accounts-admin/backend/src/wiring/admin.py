"""The model views the admin panel registers."""

from ..modules.user.admin import UserAdmin

ADMIN_VIEWS: tuple[type, ...] = (
    UserAdmin,
)
