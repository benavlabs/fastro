"""The admin site is mounted."""

from src.wiring import admin

from src import app


def test_the_admin_site_is_reexported() -> None:
    assert (admin.SITE, app.site()) == ("admin", "admin")
