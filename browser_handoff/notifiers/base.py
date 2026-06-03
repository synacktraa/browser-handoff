"""Base notifier class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from .message import LinkItem, MessageItem, TextItem

Urgency = Literal["low", "normal", "high", "critical"]


class Notifier(ABC):
    """Abstract base class for notification delivery.

    Subclasses receive a list of structured `MessageItem`s in `send()` and
    render them with channel-native primitives. A plain string is also
    accepted for back-compat and is wrapped as a single `TextItem` by
    `_normalize_items`.
    """

    notifier_type: str = "base"

    @abstractmethod
    async def send(
        self,
        title: str,
        message: str | list[MessageItem],
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        """Send a notification.

        Args:
            title: Notification title/subject.
            message: Either a plain string (rendered as a single paragraph)
                or a list of `MessageItem`s the notifier renders natively.
            urgency: Urgency level of the notification.
            **kwargs: Additional notifier-specific options.

        Returns:
            True if notification was sent successfully.
        """
        pass

    @staticmethod
    def _normalize_items(message: str | list[MessageItem]) -> list[MessageItem]:
        """Coerce the polymorphic `message` argument to a list of items so
        subclasses can always iterate without type-checking the input.
        """
        if isinstance(message, str):
            return [TextItem(message)]
        return list(message)

    @staticmethod
    def _items_to_plain_text(items: list[MessageItem]) -> str:
        """Flatten items to a plain string. Channels with no native rich
        rendering (basic SMTP plain part, fallback subclasses) can call
        this instead of writing their own.
        """
        parts: list[str] = []
        for item in items:
            if isinstance(item, TextItem):
                parts.append(item.text)
            elif isinstance(item, LinkItem):
                parts.append(f"{item.prefix}{item.url}{item.suffix}")
        return "\n\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Serialize notifier to dictionary."""
        return {"type": self.notifier_type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notifier":
        """Create notifier from dictionary.

        This should be overridden by subclasses.
        """
        raise NotImplementedError("Subclasses must implement from_dict")
