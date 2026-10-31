"""Billing mounts its router ahead of accounts'."""

from src.modules.billing import routes

from src import app


def test_billing_has_a_router() -> None:
    assert routes.router == "billing"


def test_billing_mounts_before_accounts() -> None:
    prefixes = app.prefixes()

    assert prefixes.index("/billing") < prefixes.index("/accounts")
