"""Notifiers for browser-handoff."""

from __future__ import annotations

from typing import Any

from .base import Notifier, Urgency
from .console import ConsoleNotifier
from .discord import DiscordNotifier
from .email import EmailNotifier
from .message import LinkItem, MessageItem, TextItem
from .slack import SlackNotifier


def notifier_from_dict(data: dict[str, Any]) -> Notifier:
    """Create a notifier from dictionary representation.

    Args:
        data: Dictionary with 'type' key and type-specific fields.

    Returns:
        The appropriate notifier instance.

    Raises:
        ValueError: If the notifier type is unknown.
    """
    notifier_type = data.get("type")

    if notifier_type == "console":
        return ConsoleNotifier.from_dict(data)
    elif notifier_type == "slack":
        return SlackNotifier.from_dict(data)
    elif notifier_type == "discord":
        return DiscordNotifier.from_dict(data)
    elif notifier_type == "email":
        return EmailNotifier.from_dict(data)
    else:
        raise ValueError(f"Unknown notifier type: {notifier_type}")


__all__ = [
    "Notifier",
    "Urgency",
    "ConsoleNotifier",
    "SlackNotifier",
    "DiscordNotifier",
    "EmailNotifier",
    "MessageItem",
    "TextItem",
    "LinkItem",
    "notifier_from_dict",
]
