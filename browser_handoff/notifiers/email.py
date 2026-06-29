"""Email notifier via SMTP."""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import smtplib
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from .base import Notifier, Urgency
from .message import LinkItem, MessageItem, TextItem

logger = logging.getLogger(__name__)


@dataclass
class EmailNotifier(Notifier):
    """SMTP email notifier (multipart plain+HTML).

    Example:
        EmailNotifier(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            username="bot@example.com",
            password="app-password",
            to=["ops@example.com"],
        )
    """

    notifier_type: str = field(default="email", init=False)

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to: list[str] = field(default_factory=list)
    use_tls: bool = True

    async def send(
        self,
        title: str,
        message: str | list[MessageItem],
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        """Send a multipart email.

        Args:
            title: Email subject.
            message: Plain string or list of structured message items.
            urgency: Prefixes the subject ([Info] / [Important] / [URGENT]).
            **kwargs: Unused.
        """
        if not self.to:
            logger.warning("EmailNotifier: No recipients configured")
            return False

        if not self.username or not self.password:
            logger.warning("EmailNotifier: No credentials configured")
            return False

        prefix_map = {
            "low": "[Info]",
            "normal": "",
            "high": "[Important]",
            "critical": "[URGENT]",
        }
        prefix = prefix_map.get(urgency, "")
        subject = f"{prefix} {title}".strip()

        items = self._normalize_items(message)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr or self.username
        msg["To"] = ", ".join(self.to)

        msg.attach(MIMEText(self._items_to_plain_text(items), "plain"))
        msg.attach(MIMEText(self._build_html(title, items), "html"))

        # smtplib blocks; run it off the event loop.
        try:
            await asyncio.to_thread(self._send_sync, msg)
            return True
        except Exception as e:
            logger.error(f"EmailNotifier: Failed to send email: {e}")
            return False

    @staticmethod
    def _build_html(title: str, items: list[MessageItem]) -> str:
        """Render the HTML body, escaping every user-supplied string."""
        safe_title = html_lib.escape(title)
        body_parts: list[str] = []
        for item in items:
            if isinstance(item, TextItem):
                body_parts.append(f"<p>{html_lib.escape(item.text)}</p>")
            elif isinstance(item, LinkItem):
                href = html_lib.escape(item.url, quote=True)
                body_parts.append(
                    "<p>"
                    f"{html_lib.escape(item.prefix)}"
                    f'<a href="{href}">{html_lib.escape(item.url)}</a>'
                    f"{html_lib.escape(item.suffix)}"
                    "</p>"
                )
        return f"<html><body><h2>{safe_title}</h2>{''.join(body_parts)}</body></html>"

    def _send_sync(self, msg: MIMEMultipart) -> None:
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            if self.use_tls:
                server.starttls()
            server.login(self.username, self.password)
            server.sendmail(
                self.from_addr or self.username,
                self.to,
                msg.as_string(),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.notifier_type,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "username": self.username,
            "password": self.password,
            "from_addr": self.from_addr,
            "to": self.to,
            "use_tls": self.use_tls,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmailNotifier":
        return cls(
            smtp_host=data.get("smtp_host", "smtp.gmail.com"),
            smtp_port=data.get("smtp_port", 587),
            username=data.get("username", ""),
            password=data.get("password", ""),
            from_addr=data.get("from_addr", ""),
            to=data.get("to", []),
            use_tls=data.get("use_tls", True),
        )
