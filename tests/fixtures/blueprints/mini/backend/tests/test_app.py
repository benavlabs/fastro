"""The app puts its wiring together."""

from src import app


def test_routers_mount_in_order() -> None:
    assert app.prefixes() == ["/billing", "/accounts"]


def test_the_app_is_ready() -> None:
    assert app.ready()


def test_the_admin_site_and_task_modules_are_wired() -> None:
    assert (app.site(), app.task_modules()) == ("admin", ["src.modules.tasks.jobs"])
