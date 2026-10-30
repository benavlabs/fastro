"""The background jobs run."""

from src.modules.tasks import jobs


def test_cleanup_runs() -> None:
    assert jobs.cleanup() == "cleaned"
