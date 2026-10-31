"""The model views the admin panel registers."""

from ..modules.role.admin import RoleAdmin, RolePermissionAdmin, UserRoleAdmin
from ..modules.tier.admin import TierAdmin
from ..modules.user.admin import UserAdmin

ADMIN_VIEWS: tuple[type, ...] = (
    UserAdmin,
    TierAdmin,
    RoleAdmin,
    RolePermissionAdmin,
    UserRoleAdmin,
)
