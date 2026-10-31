"""The sender the account emails go out through."""

from crudauth import EmailSender

from ..infrastructure.email.factory import build_sender
from ..infrastructure.email.senders import HandingOverSender

EMAIL_SENDER: EmailSender = HandingOverSender(build_sender())
