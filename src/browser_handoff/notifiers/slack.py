"""Slack webhook notifier."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request, urlopen

from .base import Notifier, Urgency

logger = logging.getLogger(__name__)


@dataclass
class SlackNotifier(Notifier):
    """Notifier that sends messages via Slack webhook.

    Example:
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.com/services/T.../B.../xxx"
        )
        await notifier.send(
            title="Intervention Required",
            message="Click here to help: https://...",
            urgency="critical",
        )
    """

    notifier_type: str = field(default="slack", init=False)

    webhook_url: str = ""
    channel: str | None = None  # Optional channel override
    username: str = "Browser Handoff"
    icon_emoji: str = ":robot_face:"

    async def send(
        self,
        title: str,
        message: str,
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        """Send a Slack notification via webhook.

        Args:
            title: Message title (shown in bold).
            message: Message body.
            urgency: Urgency level (affects emoji and color).
            **kwargs: Additional options.

        Returns:
            True if sent successfully.
        """
        if not self.webhook_url:
            logger.warning("SlackNotifier: No webhook_url configured")
            return False

        # Map urgency to color
        color_map = {
            "low": "#36a64f",  # green
            "normal": "#2196f3",  # blue
            "high": "#ff9800",  # orange
            "critical": "#f44336",  # red
        }

        # Map urgency to emoji prefix
        emoji_map = {
            "low": ":information_source:",
            "normal": ":bell:",
            "high": ":warning:",
            "critical": ":rotating_light:",
        }

        color = color_map.get(urgency, "#2196f3")
        emoji = emoji_map.get(urgency, ":bell:")

        payload: dict[str, Any] = {
            "username": self.username,
            "icon_emoji": self.icon_emoji,
            "attachments": [
                {
                    "color": color,
                    "title": f"{emoji} {title}",
                    "text": message,
                    "mrkdwn_in": ["text"],
                }
            ],
        }

        if self.channel:
            payload["channel"] = self.channel

        try:
            request = Request(
                self.webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=10) as response:
                return response.status == 200
        except Exception as e:
            logger.error(f"SlackNotifier: Failed to send notification: {e}")
            return False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        result: dict[str, Any] = {
            "type": self.notifier_type,
            "webhook_url": self.webhook_url,
        }
        if self.channel:
            result["channel"] = self.channel
        if self.username != "Browser Handoff":
            result["username"] = self.username
        if self.icon_emoji != ":robot_face:":
            result["icon_emoji"] = self.icon_emoji
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SlackNotifier":
        """Create from dictionary."""
        return cls(
            webhook_url=data.get("webhook_url", ""),
            channel=data.get("channel"),
            username=data.get("username", "Browser Handoff"),
            icon_emoji=data.get("icon_emoji", ":robot_face:"),
        )
