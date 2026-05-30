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

logger = logging.getLogger(__name__)


@dataclass
class EmailNotifier(Notifier):
    """Notifier that sends emails via SMTP.

    Example:
        notifier = EmailNotifier(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            username="bot@example.com",
            password="app-password",
            from_addr="bot@example.com",
            to=["ops@example.com"],
        )
        await notifier.send(
            title="Intervention Required",
            message="Click here to help: https://...",
            urgency="critical",
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
        message: str,
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        """Send an email notification.

        Args:
            title: Email subject.
            message: Email body (plain text).
            urgency: Urgency level (adds prefix to subject).
            **kwargs: Additional options.

        Returns:
            True if sent successfully.
        """
        if not self.to:
            logger.warning("EmailNotifier: No recipients configured")
            return False

        if not self.username or not self.password:
            logger.warning("EmailNotifier: No credentials configured")
            return False

        # Add urgency prefix to subject
        prefix_map = {
            "low": "[Info]",
            "normal": "",
            "high": "[Important]",
            "critical": "[URGENT]",
        }
        prefix = prefix_map.get(urgency, "")
        subject = f"{prefix} {title}".strip()

        # Create message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr or self.username
        msg["To"] = ", ".join(self.to)

        # Plain text part
        msg.attach(MIMEText(message, "plain"))

        # HTML part with basic formatting. Title and message are caller-supplied
        # strings (often containing URLs/reasons from upstream input), so they
        # are escaped before interpolation to prevent HTML injection.
        msg.attach(MIMEText(self._build_html(title, message), "html"))

        # Send in executor to avoid blocking the event loop.
        try:
            await asyncio.to_thread(self._send_sync, msg)
            return True
        except Exception as e:
            logger.error(f"EmailNotifier: Failed to send email: {e}")
            return False

    @staticmethod
    def _build_html(title: str, message: str) -> str:
        """Render the HTML body with `title` and `message` HTML-escaped."""
        safe_title = html_lib.escape(title)
        safe_message = html_lib.escape(message)
        return (
            "<html><body>"
            f"<h2>{safe_title}</h2>"
            f'<pre style="font-family: sans-serif; white-space: pre-wrap;">'
            f"{safe_message}</pre>"
            "</body></html>"
        )

    def _send_sync(self, msg: MIMEMultipart) -> None:
        """Synchronous send operation."""
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
        """Serialize to dictionary."""
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
        """Create from dictionary."""
        return cls(
            smtp_host=data.get("smtp_host", "smtp.gmail.com"),
            smtp_port=data.get("smtp_port", 587),
            username=data.get("username", ""),
            password=data.get("password", ""),
            from_addr=data.get("from_addr", ""),
            to=data.get("to", []),
            use_tls=data.get("use_tls", True),
        )
