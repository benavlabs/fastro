"""The admin site is mounted."""

from src.wiring import admin


def test_the_admin_site_is_reexported() -> None:
    assert admin.SITE == "admin"
