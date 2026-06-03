"""Discord webhook notifier."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .base import Notifier, Urgency
from .message import LinkItem, MessageItem, TextItem

logger = logging.getLogger(__name__)


@dataclass
class DiscordNotifier(Notifier):
    """Notifier that sends messages via Discord webhook.

    Example:
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc..."
        )
        await notifier.send(
            title="Intervention Required",
            message="Click here to help: https://...",
            urgency="critical",
        )
    """

    notifier_type: str = field(default="discord", init=False)

    webhook_url: str = ""
    username: str = "Browser Handoff"
    avatar_url: str | None = None

    async def send(
        self,
        title: str,
        message: str | list[MessageItem],
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        """Send a Discord notification via webhook.

        Args:
            title: Message title (shown in embed).
            message: Plain string or list of structured message items.
            urgency: Urgency level (affects embed color).
            **kwargs: Additional options.

        Returns:
            True if sent successfully.
        """
        if not self.webhook_url:
            logger.warning("DiscordNotifier: No webhook_url configured")
            return False

        # Map urgency to embed color (Discord uses decimal color values)
        color_map = {
            "low": 3066993,      # green (#2ecc71)
            "normal": 3447003,   # blue (#3498db)
            "high": 15105570,    # orange (#e67e22)
            "critical": 15158332, # red (#e74c3c)
        }

        # Map urgency to emoji prefix
        emoji_map = {
            "low": ":information_source:",
            "normal": ":bell:",
            "high": ":warning:",
            "critical": ":rotating_light:",
        }

        color = color_map.get(urgency, 3447003)
        emoji = emoji_map.get(urgency, ":bell:")

        items = self._normalize_items(message)

        # Render items to Discord-flavoured Markdown. Bare URLs auto-link
        # in the embed description.
        description_parts: list[str] = []
        for item in items:
            if isinstance(item, TextItem):
                description_parts.append(item.text)
            elif isinstance(item, LinkItem):
                description_parts.append(f"{item.prefix}{item.url}{item.suffix}")
        description = "\n\n".join(description_parts)

        # Promote the first LinkItem to the embed's `url` field so the
        # title becomes a clickable hyperlink — most prominent place to
        # surface the stream URL.
        first_link = next((i for i in items if isinstance(i, LinkItem)), None)

        embed: dict[str, Any] = {
            "title": f"{emoji} {title}",
            "description": description,
            "color": color,
        }
        if first_link is not None:
            embed["url"] = first_link.url

        payload: dict[str, Any] = {
            "username": self.username,
            "embeds": [embed],
        }

        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url

        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "BrowserHandoff/1.0",
            },
            method="POST",
        )

        def do_send() -> int:
            with urlopen(request, timeout=30) as response:
                return response.status

        try:
            status = await asyncio.to_thread(do_send)
            # Discord returns 204 No Content on success
            return status in (200, 204)
        except HTTPError as e:
            # Try to read response body for more details
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass

            if e.code == 403:
                logger.error(
                    "DiscordNotifier: Webhook returned 403 Forbidden. "
                    "The webhook URL may be invalid or revoked. "
                    f"Response: {error_body}"
                )
            elif e.code == 404:
                logger.error(
                    "DiscordNotifier: Webhook not found (404). "
                    f"The webhook may have been deleted. Response: {error_body}"
                )
            elif e.code == 429:
                logger.error(
                    "DiscordNotifier: Rate limited by Discord (429). "
                    f"Too many requests sent. Response: {error_body}"
                )
            else:
                logger.error(
                    f"DiscordNotifier: HTTP error {e.code}: {e.reason}. "
                    f"Response: {error_body}"
                )
            return False
        except Exception as e:
            logger.error(f"DiscordNotifier: Failed to send notification: {e}")
            return False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        result: dict[str, Any] = {
            "type": self.notifier_type,
            "webhook_url": self.webhook_url,
        }
        if self.username != "Browser Handoff":
            result["username"] = self.username
        if self.avatar_url:
            result["avatar_url"] = self.avatar_url
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscordNotifier":
        """Create from dictionary."""
        return cls(
            webhook_url=data.get("webhook_url", ""),
            username=data.get("username", "Browser Handoff"),
            avatar_url=data.get("avatar_url"),
        )
