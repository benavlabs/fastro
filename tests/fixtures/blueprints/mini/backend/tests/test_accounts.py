"""Accounts mounts its router and sends email."""

from src.modules.accounts import email
from src.wiring import app as wiring

from src import app


def test_accounts_mounts_its_router() -> None:
    assert "/accounts" in app.prefixes()


def test_accounts_sends_email() -> None:
    sent = len(email.SENT)

    wiring.EMAIL_SENDER("hello")

    assert len(email.SENT) == sent + 1
