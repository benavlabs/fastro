"""What every selection of mini has: an app that imports its wiring and is ready."""

from src import app


def test_the_app_is_ready() -> None:
    assert app.ready()


def test_the_wiring_is_there_whatever_is_selected() -> None:
    assert (type(app.prefixes()), type(app.site()), type(app.task_modules())) == (list, str, list)
