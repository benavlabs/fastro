"""The background jobs run, and the app knows where they are."""

from src.modules.tasks import jobs

from src import app


def test_cleanup_runs() -> None:
    assert jobs.cleanup() == "cleaned"


def test_the_task_modules_are_wired() -> None:
    assert app.task_modules() == ["src.modules.tasks.jobs"]
