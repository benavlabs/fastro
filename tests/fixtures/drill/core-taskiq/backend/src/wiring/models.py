"""Extension points of the user model: what other features add to ``User``."""

from pydantic import BaseModel


class UserModelExtensions():
    """Columns and relationships other features add to ``User``."""


class UserSchemaExtensions(BaseModel):
    """Fields other features add to the user read schemas."""
