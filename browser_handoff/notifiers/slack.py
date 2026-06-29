"""Slack webhook notifier."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request, urlopen

from .base import Notifier, Urgency
from .message import LinkItem, MessageItem, TextItem

logger = logging.getLogger(__name__)


@dataclass
class SlackNotifier(Notifier):
    """Slack notifier via incoming webhook.

    Example:
        SlackNotifier(webhook_url="https://hooks.slack.com/services/T.../B.../xxx")
    """

    notifier_type: str = field(default="slack", init=False)

    webhook_url: str = ""
    channel: str | None = None
    username: str = "Browser Handoff"
    icon_emoji: str = ":robot_face:"

    async def send(
        self,
        title: str,
        message: str | list[MessageItem],
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        """Post the notification via the configured webhook.

        Args:
            title: Message title (shown in bold).
            message: Plain string or list of structured message items.
            urgency: Picks the emoji + color (low/normal/high/critical).
            **kwargs: Unused.
        """
        if not self.webhook_url:
            logger.warning("SlackNotifier: No webhook_url configured")
            return False

        color_map = {
            "low": "#36a64f",
            "normal": "#2196f3",
            "high": "#ff9800",
            "critical": "#f44336",
        }
        emoji_map = {
            "low": ":information_source:",
            "normal": ":bell:",
            "high": ":warning:",
            "critical": ":rotating_light:",
        }

        color = color_map.get(urgency, "#2196f3")
        emoji = emoji_map.get(urgency, ":bell:")

        items = self._normalize_items(message)

        # Slack mrkdwn `<url|label>` with url as label — handles long
        # URLs gracefully without breaking line selection.
        parts: list[str] = []
        for item in items:
            if isinstance(item, TextItem):
                parts.append(item.text)
            elif isinstance(item, LinkItem):
                hyperlink = f"<{item.url}|{item.url}>"
                parts.append(f"{item.prefix}{hyperlink}{item.suffix}")
        text = "\n\n".join(parts)

        payload: dict[str, Any] = {
            "username": self.username,
            "icon_emoji": self.icon_emoji,
            "attachments": [
                {
                    "color": color,
                    "title": f"{emoji} {title}",
                    "text": text,
                    "mrkdwn_in": ["text"],
                }
            ],
        }

        if self.channel:
            payload["channel"] = self.channel

        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def do_send() -> int:
            with urlopen(request, timeout=10) as response:
                return response.status

        try:
            status = await asyncio.to_thread(do_send)
            return status == 200
        except Exception as e:
            logger.error(f"SlackNotifier: Failed to send notification: {e}")
            return False

    def to_dict(self) -> dict[str, Any]:
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
        return cls(
            webhook_url=data.get("webhook_url", ""),
            channel=data.get("channel"),
            username=data.get("username", "Browser Handoff"),
            icon_emoji=data.get("icon_emoji", ":robot_face:"),
        )
