"""Tests for Handoff class."""

import json
import pytest

from browser_handoff import (
    Handoff,
    HandoffResult,
    Scenario,
    ServerConfig,
)
from browser_handoff.detection import Detection
from browser_handoff.notifiers import SlackNotifier


class TestHandoffCreation:
    """Tests for Handoff class creation."""

    def test_requires_scenarios(self):
        """Test that Handoff requires at least one scenario."""
        with pytest.raises(ValueError, match="At least one scenario must be provided"):
            Handoff()

    def test_requires_scenarios_empty_list(self):
        """Test that Handoff requires at least one scenario (empty list)."""
        with pytest.raises(ValueError, match="At least one scenario must be provided"):
            Handoff(scenarios=[])

    def test_programmatic_creation(self):
        """Test creating Handoff programmatically."""
        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="cloudflare",
                    trigger=Detection.content(title_contains=["Just a moment"]),
                    complete=Detection.element(missing=[".cf-turnstile"]),
                ),
                Scenario(
                    name="captcha",
                    trigger=Detection.element(present=[".captcha"]),
                    complete=Detection.element(missing=[".captcha"]),
                ),
            ],
            server=ServerConfig(port=3000),
            notifiers=[
                SlackNotifier(webhook_url="https://hooks.slack.com/test"),
            ],
        )
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "cloudflare"
        assert handoff.scenarios[1].name == "captcha"
        assert handoff.server.port == 3000
        assert len(handoff.notifiers) == 1

    def test_from_dict(self):
        """Test creating Handoff from dictionary."""
        config = {
            "scenarios": [
                {
                    "name": "challenge",
                    "trigger": {"type": "content", "title_contains": ["Challenge"]},
                    "complete": {"type": "url", "path_matches": ["/callback"]},
                },
            ],
            "server": {
                "port": 8080,
                "completion_timeout": 300,
            },
            "notifiers": [
                {"type": "slack", "webhook_url": "https://test.com/webhook"},
            ],
        }
        handoff = Handoff.from_dict(config)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "challenge"
        assert handoff.server.port == 8080
        assert handoff.server.completion_timeout == 300
        assert len(handoff.notifiers) == 1

    def test_from_dict_requires_scenarios(self):
        """Test that from_dict requires scenarios."""
        with pytest.raises(ValueError, match="At least one scenario must be provided"):
            Handoff.from_dict({})

    def test_from_json(self):
        """Test creating Handoff from JSON string."""
        json_str = json.dumps({
            "scenarios": [
                {
                    "name": "captcha",
                    "trigger": {"type": "element", "present": ["#captcha"]},
                    "complete": {"type": "content", "body_contains": ["Success"]},
                },
            ],
        })
        handoff = Handoff.from_json(json_str)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "captcha"

    def test_from_yaml(self):
        """Test creating Handoff from YAML string."""
        yaml_str = """
scenarios:
  - name: security_check
    trigger:
      type: content
      title_contains:
        - "Security Check"
    complete:
      type: url
      host_equals:
        - localhost
"""
        handoff = Handoff.from_yaml(yaml_str)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "security_check"

    def test_from_file_json(self, tmp_path):
        """Test loading from JSON file."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "scenarios": [
                {
                    "name": "test_scenario",
                    "trigger": {"type": "content", "title_contains": ["Test"]},
                    "complete": {"type": "url", "path_matches": ["/done"]},
                },
            ],
        }))
        handoff = Handoff.from_file(config_file)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "test_scenario"

    def test_from_file_yaml(self, tmp_path):
        """Test loading from YAML file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
scenarios:
  - name: blocker
    trigger:
      type: element
      present:
        - ".blocker"
    complete:
      type: element
      present:
        - "#success"
""")
        handoff = Handoff.from_file(config_file)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "blocker"


class TestHandoffWithEnvVars:
    """Tests for Handoff with environment variable interpolation."""

    def test_env_var_in_server_config(self, monkeypatch, tmp_path):
        """Test env var interpolation in server config."""
        monkeypatch.setenv("HANDOFF_PORT", "9000")
        monkeypatch.setenv("HANDOFF_URL", "https://proxy.example.com")

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "scenarios": [
                {
                    "name": "test",
                    "trigger": {"type": "content", "title_contains": ["Test"]},
                    "complete": {"type": "content", "body_contains": ["Done"]},
                },
            ],
            "server": {
                "port": "${HANDOFF_PORT}",
                "public_base": "${HANDOFF_URL}",
            },
        }))

        handoff = Handoff.from_file(config_file)
        assert handoff.server.public_base == "https://proxy.example.com"

    def test_env_var_in_notifiers(self, monkeypatch, tmp_path):
        """Test env var interpolation in notifier config."""
        monkeypatch.setenv("SLACK_WEBHOOK", "https://hooks.slack.com/secret")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
scenarios:
  - name: test
    trigger:
      type: content
      title_contains:
        - Test
    complete:
      type: content
      body_contains:
        - Done
notifiers:
  - type: slack
    webhook_url: ${SLACK_WEBHOOK}
""")
        handoff = Handoff.from_file(config_file)
        assert len(handoff.notifiers) == 1
        assert handoff.notifiers[0].webhook_url == "https://hooks.slack.com/secret"


class TestHandoffResult:
    """Tests for the flattened HandoffResult dataclass."""

    def test_not_blocked(self):
        result = HandoffResult(was_blocked=False)
        assert result.was_blocked is False
        assert result.timed_out is False
        assert result.scenario_name is None
        assert result.trigger_reason is None
        assert result.completion_reason is None
        assert result.duration == 0.0

    def test_completed(self):
        result = HandoffResult(
            was_blocked=True,
            timed_out=False,
            scenario_name="login_required",
            trigger_reason="Login form detected",
            completion_reason="URL matched /dashboard",
            duration=10.5,
        )
        assert result.was_blocked is True
        assert result.timed_out is False
        assert result.scenario_name == "login_required"
        assert result.trigger_reason == "Login form detected"
        assert result.completion_reason == "URL matched /dashboard"
        assert result.duration == 10.5

    def test_timed_out(self):
        result = HandoffResult(
            was_blocked=True,
            timed_out=True,
            scenario_name="login_required",
            trigger_reason="Login form detected",
            completion_reason=None,
            duration=600.0,
        )
        assert result.was_blocked is True
        assert result.timed_out is True
        assert result.completion_reason is None


class TestComplexConfig:
    """Tests for complex configuration scenarios."""

    def test_nested_combinators(self):
        """Test loading config with nested combinators."""
        config = {
            "scenarios": [
                {
                    "name": "complex_trigger",
                    "trigger": {
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
                    "complete": {
                        "type": "all",
                        "conditions": [
                            {"type": "url", "query_contains": ["code="]},
                            {
                                "type": "not",
                                "condition": {"type": "element", "present": [".error"]},
                            },
                        ],
                    },
                },
            ],
        }
        handoff = Handoff.from_dict(config)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "complex_trigger"

    def test_multiple_notifiers(self):
        """Test loading config with multiple notifiers."""
        config = {
            "scenarios": [
                {
                    "name": "test",
                    "trigger": {"type": "content", "title_contains": ["Test"]},
                    "complete": {"type": "content", "body_contains": ["Done"]},
                },
            ],
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


class TestScenario:
    """Tests for Scenario class."""

    def test_scenario_creation(self):
        """Test creating a Scenario programmatically."""
        scenario = Scenario(
            name="cloudflare_challenge",
            trigger=Detection.content(title_contains=["Just a moment"]),
            complete=Detection.element(missing=[".cf-turnstile"]),
        )
        assert scenario.name == "cloudflare_challenge"
        assert scenario.trigger is not None
        assert scenario.complete is not None

    def test_scenario_to_dict(self):
        """Test serializing a Scenario to dictionary."""
        scenario = Scenario(
            name="login_required",
            trigger=Detection.url(path_contains=["/login"]),
            complete=Detection.url(path_contains=["/dashboard"]),
        )
        data = scenario.to_dict()
        assert data["name"] == "login_required"
        assert "trigger" in data
        assert "complete" in data
        assert data["trigger"]["type"] == "url"
        assert data["complete"]["type"] == "url"

    def test_scenario_from_dict(self):
        """Test creating a Scenario from dictionary."""
        data = {
            "name": "captcha_challenge",
            "trigger": {"type": "element", "present": ["#captcha"]},
            "complete": {"type": "element", "missing": ["#captcha"]},
        }
        scenario = Scenario.from_dict(data)
        assert scenario.name == "captcha_challenge"
        assert scenario.trigger is not None
        assert scenario.complete is not None

    def test_scenario_from_dict_unnamed(self):
        """Test creating a Scenario without name defaults to 'unnamed'."""
        data = {
            "trigger": {"type": "content", "title_contains": ["Challenge"]},
            "complete": {"type": "content", "body_contains": ["Success"]},
        }
        scenario = Scenario.from_dict(data)
        assert scenario.name == "unnamed"


class TestHandoffWithScenarios:
    """Tests for Handoff with multiple scenarios."""

    def test_handoff_with_multiple_scenarios(self):
        """Test creating Handoff with multiple scenarios."""
        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="cloudflare_challenge",
                    trigger=Detection.content(title_contains=["Just a moment"]),
                    complete=Detection.element(missing=[".cf-turnstile"]),
                ),
                Scenario(
                    name="login_required",
                    trigger=Detection.url(path_contains=["/login"]),
                    complete=Detection.url(path_contains=["/dashboard"]),
                ),
            ],
            server=ServerConfig(port=8080),
        )
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "cloudflare_challenge"
        assert handoff.scenarios[1].name == "login_required"

    def test_handoff_with_scenarios_from_dict(self):
        """Test creating Handoff with scenarios from dictionary."""
        config = {
            "scenarios": [
                {
                    "name": "cloudflare_challenge",
                    "trigger": {"type": "content", "title_contains": ["Just a moment"]},
                    "complete": {"type": "element", "missing": [".cf-turnstile"]},
                },
                {
                    "name": "recaptcha",
                    "trigger": {"type": "element", "present": [".g-recaptcha"]},
                    "complete": {"type": "element", "missing": [".g-recaptcha"]},
                },
            ],
            "server": {"port": 9000},
        }
        handoff = Handoff.from_dict(config)
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "cloudflare_challenge"
        assert handoff.scenarios[1].name == "recaptcha"

    def test_handoff_with_scenarios_from_json(self):
        """Test creating Handoff with scenarios from JSON string."""
        json_str = json.dumps({
            "scenarios": [
                {
                    "name": "auth_flow",
                    "trigger": {"type": "url", "host_equals": ["accounts.google.com"]},
                    "complete": {"type": "url", "query_contains": ["code="]},
                },
            ],
        })
        handoff = Handoff.from_json(json_str)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "auth_flow"

    def test_handoff_with_scenarios_from_yaml(self):
        """Test creating Handoff with scenarios from YAML string."""
        yaml_str = """
scenarios:
  - name: cloudflare_challenge
    trigger:
      type: content
      title_contains:
        - "Just a moment"
    complete:
      type: element
      missing:
        - ".cf-turnstile"
  - name: hcaptcha
    trigger:
      type: element
      present:
        - ".h-captcha"
    complete:
      type: element
      missing:
        - ".h-captcha"
server:
  port: 8080
  completion_timeout: 600
"""
        handoff = Handoff.from_yaml(yaml_str)
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "cloudflare_challenge"
        assert handoff.scenarios[1].name == "hcaptcha"
        assert handoff.server.port == 8080

    def test_handoff_with_scenarios_from_file(self, tmp_path):
        """Test loading Handoff with scenarios from file."""
        config_file = tmp_path / "scenarios.yaml"
        config_file.write_text("""
scenarios:
  - name: cloudflare_turnstile
    trigger:
      type: all
      conditions:
        - type: content
          title_contains:
            - "Just a moment"
        - type: element
          present:
            - ".cf-turnstile"
    complete:
      type: element
      missing:
        - ".cf-turnstile"

  - name: google_oauth
    trigger:
      type: url
      host_equals:
        - accounts.google.com
    complete:
      type: url
      host_equals:
        - localhost
      query_contains:
        - "code="

server:
  port: 8080
  completion_timeout: 300

notifiers:
  - type: slack
    webhook_url: https://hooks.slack.com/test
""")
        handoff = Handoff.from_file(config_file)
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "cloudflare_turnstile"
        assert handoff.scenarios[1].name == "google_oauth"
        assert handoff.server.port == 8080
        assert handoff.server.completion_timeout == 300
        assert len(handoff.notifiers) == 1
