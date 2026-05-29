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


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
class TestHandoffCreation:
    """Tests for Handoff class creation.

    Some tests here exercise the deprecated `scenarios=` constructor arg and
    the deprecated from_* loaders to confirm they still *work*; the warnings
    themselves are asserted separately in TestDeprecations, so this class
    silences them.
    """

    def test_construction_without_scenarios_allowed(self):
        """A Handoff is reusable transport config; scenarios are optional at
        construction and supplied per-call via run(scenarios=...)."""
        assert Handoff().scenarios == []
        assert Handoff(scenarios=[]).scenarios == []

    def test_run_requires_scenarios(self):
        """run() raises if no scenarios are passed and none are set on the
        instance. Validation happens before the page is touched, so a bare
        object stands in for the Playwright page here."""
        import asyncio

        handoff = Handoff()
        with pytest.raises(ValueError, match="requires at least one scenario"):
            asyncio.run(handoff.run(object()))

    def test_programmatic_creation(self):
        """Test creating Handoff programmatically."""
        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="login",
                    trigger=Detection.url(path_contains=["/login"]),
                    complete=Detection.url(path_contains=["/dashboard"]),
                ),
                Scenario(
                    name="payment",
                    trigger=Detection.element(present=["#card-number"]),
                    complete=Detection.url(path_contains=["/confirmation"]),
                ),
            ],
            server=ServerConfig(port=3000),
            notifiers=[
                SlackNotifier(webhook_url="https://hooks.slack.com/test"),
            ],
        )
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "login"
        assert handoff.scenarios[1].name == "payment"
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
                "session_timeout": 300,
            },
            "notifiers": [
                {"type": "slack", "webhook_url": "https://test.com/webhook"},
            ],
        }
        handoff = Handoff.from_dict(config)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "challenge"
        assert handoff.server.port == 8080
        assert handoff.server.session_timeout == 300
        assert len(handoff.notifiers) == 1

    def test_from_dict_without_scenarios_allowed(self):
        """from_dict no longer requires scenarios — an empty config yields a
        reusable Handoff whose scenarios are supplied later to run()."""
        handoff = Handoff.from_dict({})
        assert handoff.scenarios == []

    def test_from_json(self):
        """Test creating Handoff from JSON string."""
        json_str = json.dumps({
            "scenarios": [
                {
                    "name": "payment",
                    "trigger": {"type": "element", "present": ["#card-number"]},
                    "complete": {"type": "content", "body_contains": ["Order confirmed"]},
                },
            ],
        })
        handoff = Handoff.from_json(json_str)
        assert len(handoff.scenarios) == 1
        assert handoff.scenarios[0].name == "payment"

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


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
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


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
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
            name="login_required",
            trigger=Detection.url(path_contains=["/login"]),
            complete=Detection.url(path_contains=["/dashboard"]),
        )
        assert scenario.name == "login_required"
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
            "name": "password_required",
            "trigger": {"type": "element", "present": ["input[type=password]"]},
            "complete": {"type": "element", "missing": ["input[type=password]"]},
        }
        scenario = Scenario.from_dict(data)
        assert scenario.name == "password_required"
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


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
class TestHandoffWithScenarios:
    """Tests for Handoff with multiple scenarios.

    Exercises deprecated construction paths (constructor `scenarios=` and the
    from_* loaders) for functional coverage; deprecation warnings are asserted
    in TestDeprecations.
    """

    def test_handoff_with_multiple_scenarios(self):
        """Test creating Handoff with multiple scenarios."""
        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="oauth_consent",
                    trigger=Detection.content(title_contains=["Authorize"]),
                    complete=Detection.url(query_contains=["code="]),
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
        assert handoff.scenarios[0].name == "oauth_consent"
        assert handoff.scenarios[1].name == "login_required"

    def test_handoff_with_scenarios_from_dict(self):
        """Test creating Handoff with scenarios from dictionary."""
        config = {
            "scenarios": [
                {
                    "name": "login_required",
                    "trigger": {"type": "content", "title_contains": ["Sign In"]},
                    "complete": {"type": "element", "missing": ["input[type=password]"]},
                },
                {
                    "name": "payment",
                    "trigger": {"type": "element", "present": ["#card-number"]},
                    "complete": {"type": "element", "missing": ["#card-number"]},
                },
            ],
            "server": {"port": 9000},
        }
        handoff = Handoff.from_dict(config)
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "login_required"
        assert handoff.scenarios[1].name == "payment"

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
  - name: login_required
    trigger:
      type: content
      title_contains:
        - "Sign In"
    complete:
      type: element
      missing:
        - "input[type=password]"
  - name: mfa_required
    trigger:
      type: element
      present:
        - ".otp-input"
    complete:
      type: element
      missing:
        - ".otp-input"
server:
  port: 8080
  session_timeout: 600
"""
        handoff = Handoff.from_yaml(yaml_str)
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "login_required"
        assert handoff.scenarios[1].name == "mfa_required"
        assert handoff.server.port == 8080

    def test_handoff_with_scenarios_from_file(self, tmp_path):
        """Test loading Handoff with scenarios from file."""
        config_file = tmp_path / "scenarios.yaml"
        config_file.write_text("""
scenarios:
  - name: login_with_consent
    trigger:
      type: all
      conditions:
        - type: content
          title_contains:
            - "Sign In"
        - type: element
          present:
            - "input[type=password]"
    complete:
      type: element
      missing:
        - "input[type=password]"

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
  session_timeout: 300

notifiers:
  - type: slack
    webhook_url: https://hooks.slack.com/test
""")
        handoff = Handoff.from_file(config_file)
        assert len(handoff.scenarios) == 2
        assert handoff.scenarios[0].name == "login_with_consent"
        assert handoff.scenarios[1].name == "google_oauth"
        assert handoff.server.port == 8080
        assert handoff.server.session_timeout == 300
        assert len(handoff.notifiers) == 1


class TestDeprecations:
    """The deprecated construction paths must warn (and still work).

    `scenarios=` on the constructor and the Handoff.from_* loaders are slated
    for removal in the next major release. These pin the deprecation contract:
    a DeprecationWarning is emitted, the call still returns a working Handoff,
    and the from_* loaders warn exactly once (the post-construction scenario
    assignment must NOT also trip the constructor warning).
    """

    _SCENARIO = {
        "name": "login",
        "trigger": {"type": "url", "path_contains": ["/login"]},
        "complete": {"type": "url", "path_contains": ["/dashboard"]},
    }

    def test_constructor_scenarios_warns(self):
        scenario = Scenario(
            name="login",
            trigger=Detection.url(path_contains=["/login"]),
            complete=Detection.url(path_contains=["/dashboard"]),
        )
        with pytest.warns(DeprecationWarning, match="run\\(scenarios="):
            handoff = Handoff(scenarios=[scenario])
        assert handoff.scenarios == [scenario]

    def test_constructor_without_scenarios_does_not_warn(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning becomes an error
            Handoff()
            Handoff(scenarios=[])  # empty list is not "passing scenarios"

    def test_from_dict_warns(self):
        with pytest.warns(DeprecationWarning, match="from_dict"):
            handoff = Handoff.from_dict({"scenarios": [self._SCENARIO]})
        assert handoff.scenarios[0].name == "login"

    def test_from_json_warns(self):
        with pytest.warns(DeprecationWarning, match="from_json"):
            Handoff.from_json(json.dumps({"scenarios": [self._SCENARIO]}))

    def test_from_yaml_warns(self):
        with pytest.warns(DeprecationWarning, match="from_yaml"):
            Handoff.from_yaml("scenarios: []")

    def test_from_file_warns(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"scenarios": [self._SCENARIO]}))
        with pytest.warns(DeprecationWarning, match="from_file"):
            Handoff.from_file(config_file)

    def test_from_loader_warns_exactly_once(self, tmp_path):
        """A from_* loader carrying scenarios must emit ONE deprecation (its
        own), not also the constructor's — the loader sets scenarios after
        construction to avoid the double warning."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"scenarios": [self._SCENARIO]}))
        with pytest.warns(DeprecationWarning) as record:
            Handoff.from_file(config_file)
        deprecations = [w for w in record if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1, [str(w.message) for w in deprecations]
        assert "from_file" in str(deprecations[0].message)
