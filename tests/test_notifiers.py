"""Tests for notifiers."""

import pytest

from browser_handoff.notifiers import (
    ConsoleNotifier,
    DiscordNotifier,
    EmailNotifier,
    LinkItem,
    SlackNotifier,
    TextItem,
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


class TestDiscordNotifier:
    """Tests for DiscordNotifier."""

    def test_creation(self):
        """Test creating a Discord notifier."""
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            username="TestBot",
        )
        assert notifier.webhook_url == "https://discord.com/api/webhooks/123/abc"
        assert notifier.username == "TestBot"
        assert notifier.notifier_type == "discord"

    def test_default_values(self):
        """Test default values."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
        assert notifier.username == "Browser Handoff"
        assert notifier.avatar_url is None

    def test_to_dict(self):
        """Test serialization to dict."""
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            username="CustomBot",
            avatar_url="https://example.com/avatar.png",
        )
        data = notifier.to_dict()
        assert data["type"] == "discord"
        assert data["webhook_url"] == "https://discord.com/api/webhooks/123/abc"
        assert data["username"] == "CustomBot"
        assert data["avatar_url"] == "https://example.com/avatar.png"

    def test_to_dict_minimal(self):
        """Test serialization with only required fields."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
        data = notifier.to_dict()
        assert data == {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/123/abc",
        }

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/456/xyz",
            "username": "MyBot",
        }
        notifier = DiscordNotifier.from_dict(data)
        assert notifier.webhook_url == "https://discord.com/api/webhooks/456/xyz"
        assert notifier.username == "MyBot"


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

    def test_html_body_escapes_title_and_message(self):
        """Title and item content are HTML-escaped to prevent injection.

        Callers pass arbitrary upstream strings (task reasons, URLs, scenario
        names) — an unescaped `<` or `"` could break the markup or smuggle in
        attributes/tags in HTML mail clients that render them.
        """
        body = EmailNotifier._build_html(
            '<script>alert("x")</script>',
            [TextItem('click "<a href=evil>here</a>" & wait')],
        )
        # Raw injection vectors must not appear verbatim.
        assert "<script>" not in body
        assert "<a href=evil>" not in body
        # Escaped forms must appear instead.
        assert "&lt;script&gt;" in body
        assert "&lt;a href=evil&gt;" in body
        assert "&amp; wait" in body
        # The wrapping tags we control are still present.
        assert "<h2>" in body and "</h2>" in body
        assert "<p>" in body and "</p>" in body


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

    def test_discord_notifier(self):
        """Test creating Discord notifier from dict."""
        data = {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/123/abc",
        }
        notifier = notifier_from_dict(data)
        assert isinstance(notifier, DiscordNotifier)
        assert notifier.webhook_url == "https://discord.com/api/webhooks/123/abc"

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


# ---- Structured message items -----------------------------------------------
#
# Each notifier accepts either a plain string (back-compat) or a list of
# TextItem / LinkItem and renders them with channel-native primitives. The
# normalization + plain-text fallback live on the base class so subclasses
# can always operate on the structured form.


class TestNormalizeItems:
    """Notifier._normalize_items coerces the polymorphic message arg."""

    def test_str_wrapped_as_text_item(self):
        items = ConsoleNotifier._normalize_items("hello")
        assert items == [TextItem("hello")]

    def test_list_passed_through(self):
        original = [TextItem("a"), LinkItem(url="https://example.com")]
        items = ConsoleNotifier._normalize_items(original)
        assert items == original

    def test_to_plain_text_concatenates_paragraphs(self):
        items = [
            TextItem("First paragraph."),
            TextItem("Second paragraph."),
        ]
        out = ConsoleNotifier._items_to_plain_text(items)
        assert out == "First paragraph.\n\nSecond paragraph."

    def test_to_plain_text_renders_link_with_prefix_suffix(self):
        items = [
            LinkItem(prefix="Visit: ", url="https://example.com", suffix=" (now)"),
        ]
        out = ConsoleNotifier._items_to_plain_text(items)
        assert out == "Visit: https://example.com (now)"


class TestEmailHtmlRendering:
    """EmailNotifier renders TextItems as <p> and LinkItems as proper <a>."""

    def test_text_item_renders_as_paragraph(self):
        html = EmailNotifier._build_html("Title", [TextItem("body text")])
        assert "<h2>Title</h2>" in html
        assert "<p>body text</p>" in html

    def test_link_item_renders_as_anchor(self):
        html = EmailNotifier._build_html(
            "T",
            [LinkItem(prefix="Open: ", url="https://example.com/x?a=1")],
        )
        assert '<a href="https://example.com/x?a=1">' in html
        assert "Open: " in html

    def test_html_escapes_user_input(self):
        html = EmailNotifier._build_html(
            "<script>",
            [TextItem("<b>not bold</b>")],
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "<b>not bold</b>" not in html
        assert "&lt;b&gt;not bold&lt;/b&gt;" in html
