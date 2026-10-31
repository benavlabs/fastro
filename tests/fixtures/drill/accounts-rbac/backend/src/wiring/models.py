"""Extension points of the user model: what other features add to ``User``."""

from ..modules.role.contrib import UserRoleColumns
from pydantic import BaseModel


class UserModelExtensions(UserRoleColumns):
    """Columns and relationships other features add to ``User``."""


class UserSchemaExtensions(BaseModel):
    """Fields other features add to the user read schemas."""
