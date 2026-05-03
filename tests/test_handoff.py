"""Tests for Handoff class."""

import json
import pytest

from browser_handoff import (
    CompletionResult,
    Detection,
    Handoff,
    ServerConfig,
    SlackNotifier,
)


class TestHandoffCreation:
    """Tests for Handoff class creation."""

    def test_default_creation(self):
        """Test creating Handoff with defaults."""
        handoff = Handoff()
        assert handoff.trigger_on == []
        assert handoff.complete_on == []
        assert isinstance(handoff.server, ServerConfig)
        assert handoff.notifiers == []

    def test_programmatic_creation(self):
        """Test creating Handoff programmatically."""
        handoff = Handoff(
            trigger_on=[
                Detection.content(title_contains=["Just a moment"]),
                Detection.element(present=[".captcha"]),
            ],
            complete_on=[
                Detection.url(
                    host_equals=["localhost"],
                    query_contains=["code="],
                ),
            ],
            server=ServerConfig(port=3000),
            notifiers=[
                SlackNotifier(webhook_url="https://hooks.slack.com/test"),
            ],
        )
        assert len(handoff.trigger_on) == 2
        assert len(handoff.complete_on) == 1
        assert handoff.server.port == 3000
        assert len(handoff.notifiers) == 1

    def test_from_dict(self):
        """Test creating Handoff from dictionary."""
        config = {
            "trigger_on": [
                {"type": "content", "title_contains": ["Challenge"]},
            ],
            "complete_on": [
                {"type": "url", "path_matches": ["/callback"]},
            ],
            "server": {
                "port": 8080,
                "timeout": 300,
            },
            "notifiers": [
                {"type": "slack", "webhook_url": "https://test.com/webhook"},
            ],
        }
        handoff = Handoff.from_dict(config)
        assert len(handoff.trigger_on) == 1
        assert len(handoff.complete_on) == 1
        assert handoff.server.port == 8080
        assert handoff.server.timeout == 300
        assert len(handoff.notifiers) == 1

    def test_from_json(self):
        """Test creating Handoff from JSON string."""
        json_str = json.dumps({
            "trigger_on": [
                {"type": "element", "present": ["#captcha"]},
            ],
            "complete_on": [
                {"type": "content", "body_contains": ["Success"]},
            ],
        })
        handoff = Handoff.from_json(json_str)
        assert len(handoff.trigger_on) == 1
        assert len(handoff.complete_on) == 1

    def test_from_yaml(self):
        """Test creating Handoff from YAML string."""
        yaml_str = """
trigger_on:
  - type: content
    title_contains:
      - "Security Check"
complete_on:
  - type: url
    host_equals:
      - localhost
"""
        handoff = Handoff.from_yaml(yaml_str)
        assert len(handoff.trigger_on) == 1
        assert len(handoff.complete_on) == 1

    def test_from_file_json(self, tmp_path):
        """Test loading from JSON file."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "trigger_on": [{"type": "content", "title_contains": ["Test"]}],
            "complete_on": [{"type": "url", "path_matches": ["/done"]}],
        }))
        handoff = Handoff.from_file(config_file)
        assert len(handoff.trigger_on) == 1

    def test_from_file_yaml(self, tmp_path):
        """Test loading from YAML file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
trigger_on:
  - type: element
    present:
      - ".blocker"
complete_on:
  - type: element
    present:
      - "#success"
""")
        handoff = Handoff.from_file(config_file)
        assert len(handoff.trigger_on) == 1
        assert len(handoff.complete_on) == 1


class TestHandoffWithEnvVars:
    """Tests for Handoff with environment variable interpolation."""

    def test_env_var_in_server_config(self, monkeypatch, tmp_path):
        """Test env var interpolation in server config."""
        monkeypatch.setenv("HANDOFF_PORT", "9000")
        monkeypatch.setenv("HANDOFF_URL", "https://proxy.example.com")

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "trigger_on": [],
            "complete_on": [],
            "server": {
                "port": "${HANDOFF_PORT}",
                "public_base": "${HANDOFF_URL}",
            },
        }))

        # Note: port would be string after interpolation
        # Real implementation might need type coercion
        handoff = Handoff.from_file(config_file)
        assert handoff.server.public_base == "https://proxy.example.com"

    def test_env_var_in_notifiers(self, monkeypatch, tmp_path):
        """Test env var interpolation in notifier config."""
        monkeypatch.setenv("SLACK_WEBHOOK", "https://hooks.slack.com/secret")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
trigger_on: []
complete_on: []
notifiers:
  - type: slack
    webhook_url: ${SLACK_WEBHOOK}
""")
        handoff = Handoff.from_file(config_file)
        assert len(handoff.notifiers) == 1
        assert handoff.notifiers[0].webhook_url == "https://hooks.slack.com/secret"


class TestCompletionResult:
    """Tests for CompletionResult dataclass."""

    def test_creation(self):
        """Test creating a CompletionResult."""
        result = CompletionResult(
            success=True,
            reason="URL matched callback pattern",
            detection_type="url",
            duration=5.5,
        )
        assert result.success is True
        assert result.reason == "URL matched callback pattern"
        assert result.detection_type == "url"
        assert result.duration == 5.5

    def test_default_values(self):
        """Test default values."""
        result = CompletionResult(
            success=False,
            reason="Timeout",
            detection_type="timeout",
        )
        assert result.matched_detection is None
        assert result.duration == 0.0


class TestComplexConfig:
    """Tests for complex configuration scenarios."""

    def test_nested_combinators(self):
        """Test loading config with nested combinators."""
        config = {
            "trigger_on": [
                {
                    "type": "any",
                    "conditions": [
                        {"type": "content", "title_contains": ["Challenge"]},
                        {
                            "type": "all",
                            "conditions": [
                                {"type": "url", "path_contains": ["/login"]},
                                {"type": "element", "present": [".auth-form"]},
                            ],
                        },
                    ],
                },
            ],
            "complete_on": [
                {
                    "type": "all",
                    "conditions": [
                        {"type": "url", "query_contains": ["code="]},
                        {
                            "type": "not",
                            "condition": {"type": "element", "present": [".error"]},
                        },
                    ],
                },
            ],
        }
        handoff = Handoff.from_dict(config)
        assert len(handoff.trigger_on) == 1
        assert len(handoff.complete_on) == 1

    def test_multiple_notifiers(self):
        """Test loading config with multiple notifiers."""
        config = {
            "trigger_on": [],
            "complete_on": [],
            "notifiers": [
                {"type": "slack", "webhook_url": "https://slack.com/webhook1"},
                {"type": "slack", "webhook_url": "https://slack.com/webhook2"},
                {
                    "type": "email",
                    "smtp_host": "smtp.gmail.com",
                    "username": "bot@gmail.com",
                    "password": "pass",
                    "to": ["admin@example.com"],
                },
            ],
        }
        handoff = Handoff.from_dict(config)
        assert len(handoff.notifiers) == 3
