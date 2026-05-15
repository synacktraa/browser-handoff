"""Base notifier class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

Urgency = Literal["low", "normal", "high", "critical"]


class Notifier(ABC):
    """Abstract base class for notification delivery.

    Notifiers are responsible for alerting humans when intervention is needed.
    """

    notifier_type: str = "base"

    @abstractmethod
    async def send(
        self,
        title: str,
        message: str,
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        """Send a notification.

        Args:
            title: Notification title/subject.
            message: Notification body/content.
            urgency: Urgency level of the notification.
            **kwargs: Additional notifier-specific options.

        Returns:
            True if notification was sent successfully.
        """
        pass

    def to_dict(self) -> dict[str, Any]:
        """Serialize notifier to dictionary."""
        return {"type": self.notifier_type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notifier":
        """Create notifier from dictionary.

        This should be overridden by subclasses.
        """
        raise NotImplementedError("Subclasses must implement from_dict")
