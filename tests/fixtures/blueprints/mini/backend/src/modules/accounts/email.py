"""Sending email, now or through the task queue."""

SENT: list[str] = []


def send_now(message: str) -> None:
    """Send ``message`` right away."""
    SENT.append(message)


def send_queued(message: str) -> None:
    """Queue ``message`` for a background task to send."""
    SENT.append(f"queued: {message}")
