"""Billing mounts its router."""

from src.modules.billing import routes


def test_billing_has_a_router() -> None:
    assert routes.router == "billing"
