"""With tasks selected, accounts sends email through the queue."""

from src.modules.accounts import email
from src.wiring import app


def test_email_goes_through_the_queue() -> None:
    app.EMAIL_SENDER("hello")

    assert email.SENT[-1] == "queued: hello"
