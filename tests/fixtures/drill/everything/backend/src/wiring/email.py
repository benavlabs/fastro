"""The sender the account emails go out through."""

from crudauth import EmailSender

from ..infrastructure.taskiq.email import queued_sender

EMAIL_SENDER: EmailSender = queued_sender
