"""Tests for notifiers."""

import pytest

from browser_handoff.notifiers import (
    EmailNotifier,
    SlackNotifier,
    notifier_from_dict,
)


class TestSlackNotifier:
    """Tests for SlackNotifier."""

    def test_creation(self):
        """Test creating a Slack notifier."""
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.com/services/T.../B.../xxx",
            channel="#alerts",
        )
        assert notifier.webhook_url == "https://hooks.slack.com/services/T.../B.../xxx"
        assert notifier.channel == "#alerts"
        assert notifier.notifier_type == "slack"

    def test_default_values(self):
        """Test default values."""
        notifier = SlackNotifier(webhook_url="https://example.com/webhook")
        assert notifier.channel is None
        assert notifier.username == "Browser Handoff"
        assert notifier.icon_emoji == ":robot_face:"

    def test_to_dict(self):
        """Test serialization to dict."""
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.com/test",
            channel="#ops",
            username="TestBot",
        )
        data = notifier.to_dict()
        assert data["type"] == "slack"
        assert data["webhook_url"] == "https://hooks.slack.com/test"
        assert data["channel"] == "#ops"
        assert data["username"] == "TestBot"

    def test_to_dict_minimal(self):
        """Test serialization with only required fields."""
        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/test")
        data = notifier.to_dict()
        assert data == {
            "type": "slack",
            "webhook_url": "https://hooks.slack.com/test",
        }

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "slack",
            "webhook_url": "https://hooks.slack.com/services/xxx",
            "channel": "#notifications",
        }
        notifier = SlackNotifier.from_dict(data)
        assert notifier.webhook_url == "https://hooks.slack.com/services/xxx"
        assert notifier.channel == "#notifications"


class TestEmailNotifier:
    """Tests for EmailNotifier."""

    def test_creation(self):
        """Test creating an email notifier."""
        notifier = EmailNotifier(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            username="bot@example.com",
            password="secret",
            to=["ops@example.com"],
        )
        assert notifier.smtp_host == "smtp.gmail.com"
        assert notifier.smtp_port == 587
        assert notifier.username == "bot@example.com"
        assert notifier.to == ["ops@example.com"]
        assert notifier.notifier_type == "email"

    def test_default_values(self):
        """Test default values."""
        notifier = EmailNotifier()
        assert notifier.smtp_host == "smtp.gmail.com"
        assert notifier.smtp_port == 587
        assert notifier.use_tls is True

    def test_to_dict(self):
        """Test serialization to dict."""
        notifier = EmailNotifier(
            smtp_host="mail.example.com",
            smtp_port=465,
            username="user@example.com",
            password="pass123",
            from_addr="noreply@example.com",
            to=["admin@example.com", "ops@example.com"],
            use_tls=False,
        )
        data = notifier.to_dict()
        assert data["type"] == "email"
        assert data["smtp_host"] == "mail.example.com"
        assert data["smtp_port"] == 465
        assert data["to"] == ["admin@example.com", "ops@example.com"]
        assert data["use_tls"] is False

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "email",
            "smtp_host": "smtp.test.com",
            "smtp_port": 25,
            "username": "test@test.com",
            "password": "testpass",
            "to": ["recipient@test.com"],
        }
        notifier = EmailNotifier.from_dict(data)
        assert notifier.smtp_host == "smtp.test.com"
        assert notifier.smtp_port == 25
        assert notifier.to == ["recipient@test.com"]


class TestNotifierFromDict:
    """Tests for notifier_from_dict factory function."""

    def test_slack_notifier(self):
        """Test creating Slack notifier from dict."""
        data = {
            "type": "slack",
            "webhook_url": "https://hooks.slack.com/test",
        }
        notifier = notifier_from_dict(data)
        assert isinstance(notifier, SlackNotifier)
        assert notifier.webhook_url == "https://hooks.slack.com/test"

    def test_email_notifier(self):
        """Test creating email notifier from dict."""
        data = {
            "type": "email",
            "smtp_host": "smtp.example.com",
            "username": "user@example.com",
            "password": "pass",
            "to": ["admin@example.com"],
        }
        notifier = notifier_from_dict(data)
        assert isinstance(notifier, EmailNotifier)
        assert notifier.smtp_host == "smtp.example.com"

    def test_unknown_notifier_type(self):
        """Test error for unknown notifier type."""
        data = {"type": "unknown"}
        with pytest.raises(ValueError, match="Unknown notifier type"):
            notifier_from_dict(data)
