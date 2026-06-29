"""Base notifier class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from .message import LinkItem, MessageItem, TextItem

Urgency = Literal["low", "normal", "high", "critical"]


class Notifier(ABC):
    """Abstract base for notification delivery.

    Subclasses render a list of structured `MessageItem`s with
    channel-native primitives. A plain string is also accepted and
    wrapped as a single `TextItem` by `_normalize_items`.
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
        """Send a notification; return True on success.

        Args:
            title: Notification title/subject.
            message: A plain string or a list of `MessageItem`s.
            urgency: One of "low" / "normal" / "high" / "critical".
            **kwargs: Channel-specific options.
        """
        pass

    @staticmethod
    def _normalize_items(message: str | list[MessageItem]) -> list[MessageItem]:
        """Coerce `message` to a list so subclasses iterate uniformly."""
        if isinstance(message, str):
            return [TextItem(message)]
        return list(message)

    @staticmethod
    def _items_to_plain_text(items: list[MessageItem]) -> str:
        """Flatten items to a plain string for channels without rich rendering."""
        parts: list[str] = []
        for item in items:
            if isinstance(item, TextItem):
                parts.append(item.text)
            elif isinstance(item, LinkItem):
                parts.append(f"{item.prefix}{item.url}{item.suffix}")
        return "\n\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.notifier_type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notifier":
        raise NotImplementedError("Subclasses must implement from_dict")
