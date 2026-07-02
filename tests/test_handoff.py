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

    def test_guard_requires_scenarios(self):
        """guard() raises if no scenarios are passed and none are set on the
        instance. Validation happens before the page is touched, so a bare
        object stands in for the Playwright page here."""
        import asyncio

        h = Handoff()
        with pytest.raises(ValueError, match="requires at least one scenario"):
            asyncio.run(h.guard(object()))

    def test_guard_rejects_llm_in_trigger(self):
        """Triggers must not use LLMDetection — wrong prompt shape (no
        operator-facing reason), wrong activity signal (no operator yet),
        no first-event gate. Reject early at the scenario level so the
        misuse fails at the wiring site, not minutes into a hot loop.
        """
        import asyncio

        pytest.importorskip("litellm")

        h = Handoff()
        scenarios = [
            Scenario(
                name="bad-llm-trigger",
                trigger=Detection.llm(condition="login form visible"),
                complete=Detection.url(path_contains=["/done"]),
            ),
        ]
        with pytest.raises(TypeError, match="bad-llm-trigger"):
            asyncio.run(h.guard(object(), scenarios=scenarios))

    def test_guard_rejects_llm_nested_in_combinator(self):
        """The walk catches combinator nesting too — most likely accidental
        misuse since AnyOf/AllOf hide the LLMDetection in plain sight.
        """
        import asyncio

        pytest.importorskip("litellm")

        h = Handoff()
        scenarios = [
            Scenario(
                name="nested-llm-trigger",
                trigger=Detection.any([
                    Detection.url(path_contains=["/login"]),
                    Detection.llm(condition="hard-to-tell visually"),
                ]),
                complete=Detection.url(path_contains=["/done"]),
            ),
        ]
        with pytest.raises(TypeError, match="nested-llm-trigger"):
            asyncio.run(h.guard(object(), scenarios=scenarios))

    def test_programmatic_creation(self):
        """Test creating Handoff programmatically."""
        h = Handoff(
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
        assert len(h.scenarios) == 2
        assert h.scenarios[0].name == "login"
        assert h.scenarios[1].name == "payment"
        assert h.server.port == 3000
        assert len(h.notifiers) == 1

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
                "access_timeout": 120,
                "completion_timeout": 300,
            },
            "notifiers": [
                {"type": "slack", "webhook_url": "https://test.com/webhook"},
            ],
        }
        h = Handoff.from_dict(config)
        assert len(h.scenarios) == 1
        assert h.scenarios[0].name == "challenge"
        assert h.server.port == 8080
        assert h.server.access_timeout == 120
        assert h.server.completion_timeout == 300
        assert len(h.notifiers) == 1

    def test_from_dict_without_scenarios_allowed(self):
        """from_dict no longer requires scenarios — an empty config yields a
        reusable Handoff whose scenarios are supplied later to run()."""
        h = Handoff.from_dict({})
        assert h.scenarios == []

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
        h = Handoff.from_json(json_str)
        assert len(h.scenarios) == 1
        assert h.scenarios[0].name == "payment"

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
        h = Handoff.from_yaml(yaml_str)
        assert len(h.scenarios) == 1
        assert h.scenarios[0].name == "security_check"

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
        h = Handoff.from_file(config_file)
        assert len(h.scenarios) == 1
        assert h.scenarios[0].name == "test_scenario"

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
        h = Handoff.from_file(config_file)
        assert len(h.scenarios) == 1
        assert h.scenarios[0].name == "blocker"


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

        h = Handoff.from_file(config_file)
        assert h.server.public_base == "https://proxy.example.com"

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
        h = Handoff.from_file(config_file)
        assert len(h.notifiers) == 1
        assert h.notifiers[0].webhook_url == "https://hooks.slack.com/secret"


class TestHandoffResult:
    """Tests for the flattened HandoffResult dataclass."""

    def test_not_blocked(self):
        result = HandoffResult(was_blocked=False)
        assert result.was_blocked is False
        assert result.timed_out is False
        assert result.timeout_cause is None
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
        assert result.timeout_cause is None
        assert result.scenario_name == "login_required"
        assert result.trigger_reason == "Login form detected"
        assert result.completion_reason == "URL matched /dashboard"
        assert result.duration == 10.5

    def test_timed_out_access(self):
        result = HandoffResult(
            was_blocked=True,
            timed_out=True,
            timeout_cause="access",
            scenario_name="login_required",
            trigger_reason="Login form detected",
            completion_reason=None,
            duration=600.0,
        )
        assert result.was_blocked is True
        assert result.timed_out is True
        assert result.timeout_cause == "access"
        assert result.completion_reason is None

    def test_timed_out_completion(self):
        result = HandoffResult(
            was_blocked=True,
            timed_out=True,
            timeout_cause="completion",
            scenario_name="login_required",
            trigger_reason="Login form detected",
            completion_reason=None,
            duration=1800.0,
        )
        assert result.timeout_cause == "completion"


class TestResolveTimeout:
    """`_resolve_timeout` picks between per-call and config default.

    None per-call inherits the default; any other value (including
    math.inf) overrides. Pinning the layering rule directly because
    it shapes every call-site override semantics.
    """

    def test_per_call_none_inherits_default(self):
        from browser_handoff.handoff import _resolve_timeout

        assert _resolve_timeout(None, 600.0) == 600.0
        assert _resolve_timeout(None, None) is None

    def test_per_call_value_overrides_default(self):
        from browser_handoff.handoff import _resolve_timeout

        assert _resolve_timeout(5.0, 600.0) == 5.0
        # Per-call wins even when it's a "disable" sentinel.
        import math

        assert _resolve_timeout(math.inf, 600.0) == math.inf
        # Per-call wins even when the default is unbounded.
        assert _resolve_timeout(10.0, None) == 10.0


class TestAwaitTimeoutCause:
    """The three-way race returning the timeout cause (or None on match).

    Drives `Handoff._await_timeout_cause` with a stand-in session so the
    decision logic can be tested without a real browser or WS.
    """

    @staticmethod
    def _session(
        *,
        access_timeout: float | None,
        completion_timeout: float | None,
    ):
        from browser_handoff.server import SessionPresence

        class _Session:
            pass

        s = _Session()
        s.access_timeout = access_timeout
        s.completion_timeout = completion_timeout
        s.access_timer_fired = False
        s.presence = SessionPresence()
        return s

    async def test_detection_match_wins(self):
        import asyncio

        from browser_handoff import Handoff

        session = self._session(access_timeout=5.0, completion_timeout=5.0)
        completion_event = asyncio.Event()
        session.presence.bump()  # connect immediately
        completion_event.set()  # detection already matched
        result = await Handoff._await_timeout_cause(session, completion_event)
        assert result is None
        assert session.access_timer_fired is False

    async def test_access_timeout_fires_without_connect(self):
        import asyncio

        from browser_handoff import Handoff

        session = self._session(access_timeout=0.05, completion_timeout=10.0)
        completion_event = asyncio.Event()
        # Never bump presence — operator never connects.
        result = await Handoff._await_timeout_cause(session, completion_event)
        assert result == "access"
        assert session.access_timer_fired is True

    async def test_completion_timeout_fires_after_connect(self):
        import asyncio

        from browser_handoff import Handoff

        session = self._session(access_timeout=10.0, completion_timeout=0.05)
        completion_event = asyncio.Event()
        session.presence.bump()  # connected
        result = await Handoff._await_timeout_cause(session, completion_event)
        assert result == "completion"
        # Access timer was retired by the connect, not fired.
        assert session.access_timer_fired is False

    async def test_none_access_timeout_disables_access_timeout_branch(self):
        import asyncio

        from browser_handoff import Handoff

        # Access disabled at this layer; completion still bounded so the
        # race can resolve. Without a connect, completion never starts —
        # use completion_event to terminate the race.
        session = self._session(access_timeout=None, completion_timeout=None)
        completion_event = asyncio.Event()
        async def trip() -> None:
            await asyncio.sleep(0.02)
            completion_event.set()

        asyncio.create_task(trip())
        result = await asyncio.wait_for(
            Handoff._await_timeout_cause(session, completion_event),
            timeout=1.0,
        )
        assert result is None

    async def test_completion_timer_anchors_on_first_connect_only(self):
        """Bump twice; the completion_timeout sleep should start with the
        first connect and not reset on the second."""
        import asyncio
        import time

        from browser_handoff import Handoff

        session = self._session(access_timeout=10.0, completion_timeout=0.15)
        completion_event = asyncio.Event()

        async def bump_twice() -> None:
            session.presence.bump()
            await asyncio.sleep(0.05)
            session.presence.bump()  # reconnect — must not reset

        asyncio.create_task(bump_twice())
        start = time.monotonic()
        result = await Handoff._await_timeout_cause(session, completion_event)
        elapsed = time.monotonic() - start
        assert result == "completion"
        # Should fire ~0.15s after the first bump (≈0s), not after the
        # second (≈0.05s). Allow generous slack for scheduler jitter.
        assert elapsed < 0.30, f"completion timer appears to have reset (elapsed={elapsed:.3f})"


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
        h = Handoff.from_dict(config)
        assert len(h.scenarios) == 1
        assert h.scenarios[0].name == "complex_trigger"

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
        h = Handoff.from_dict(config)
        assert len(h.notifiers) == 3


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
        h = Handoff(
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
        assert len(h.scenarios) == 2
        assert h.scenarios[0].name == "oauth_consent"
        assert h.scenarios[1].name == "login_required"

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
        h = Handoff.from_dict(config)
        assert len(h.scenarios) == 2
        assert h.scenarios[0].name == "login_required"
        assert h.scenarios[1].name == "payment"

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
        h = Handoff.from_json(json_str)
        assert len(h.scenarios) == 1
        assert h.scenarios[0].name == "auth_flow"

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
  completion_timeout: 600
"""
        h = Handoff.from_yaml(yaml_str)
        assert len(h.scenarios) == 2
        assert h.scenarios[0].name == "login_required"
        assert h.scenarios[1].name == "mfa_required"
        assert h.server.port == 8080

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
  completion_timeout: 300

notifiers:
  - type: slack
    webhook_url: https://hooks.slack.com/test
""")
        h = Handoff.from_file(config_file)
        assert len(h.scenarios) == 2
        assert h.scenarios[0].name == "login_with_consent"
        assert h.scenarios[1].name == "google_oauth"
        assert h.server.port == 8080
        assert h.server.completion_timeout == 300
        assert len(h.notifiers) == 1


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
            h = Handoff(scenarios=[scenario])
        assert h.scenarios == [scenario]

    def test_constructor_without_scenarios_does_not_warn(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning becomes an error
            Handoff()
            Handoff(scenarios=[])  # empty list is not "passing scenarios"

    def test_from_dict_warns(self):
        with pytest.warns(DeprecationWarning, match="from_dict"):
            h = Handoff.from_dict({"scenarios": [self._SCENARIO]})
        assert h.scenarios[0].name == "login"

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


class TestCaptureCropMetrics:
    """`_capture_crop_metrics` reads page.evaluate(_CROP_METRICS_JS) with
    retry on degenerate values; falls back to None when retries exhaust.
    """

    async def test_returns_metrics_on_first_valid_evaluate(self):
        from browser_handoff.handoff import _capture_crop_metrics

        valid = {
            "screen_w": 1920, "screen_h": 1080,
            "page_x": 0, "page_y": 87,
            "page_w": 1920, "page_h": 993,
        }

        class FakePage:
            def __init__(self):
                self.calls = 0

            async def evaluate(self, expr):
                self.calls += 1
                return valid

        page = FakePage()
        result = await _capture_crop_metrics(page)
        assert result == valid
        assert page.calls == 1  # no retry needed on a valid first read

    async def test_returns_none_on_persistent_degenerate(self):
        from browser_handoff.handoff import _capture_crop_metrics

        zero = {
            "screen_w": 0, "screen_h": 0,
            "page_x": 0, "page_y": 0,
            "page_w": 0, "page_h": 0,
        }

        class FakePage:
            def __init__(self):
                self.calls = 0

            async def evaluate(self, expr):
                self.calls += 1
                return zero

        page = FakePage()
        result = await _capture_crop_metrics(page, attempts=3, backoff=0.0)
        assert result is None
        # Three attempts then fallback — verifies the retry loop runs.
        assert page.calls == 3

    async def test_returns_metrics_after_transient_degenerate(self):
        from browser_handoff.handoff import _capture_crop_metrics

        valid = {
            "screen_w": 1920, "screen_h": 1080,
            "page_x": 0, "page_y": 87,
            "page_w": 1920, "page_h": 993,
        }
        zero = {**valid, "screen_w": 0, "page_w": 0}

        class FakePage:
            def __init__(self):
                self.calls = 0

            async def evaluate(self, expr):
                self.calls += 1
                return zero if self.calls < 2 else valid

        page = FakePage()
        result = await _capture_crop_metrics(page, attempts=3, backoff=0.0)
        # Retry catches the transient zero and returns the next good read.
        assert result == valid
        assert page.calls == 2

    async def test_returns_none_when_evaluate_raises(self):
        from browser_handoff.handoff import _capture_crop_metrics

        class FakePage:
            async def evaluate(self, expr):
                raise RuntimeError("page detached")

        result = await _capture_crop_metrics(FakePage())
        # An evaluate raising once is fatal — no retry on raise (the page
        # is probably gone).
        assert result is None


class TestStreamUrlForwarding:
    """`stream_url` is plumbed through both Handoff.pause and
    Handoff.guard; guard() is a pass-through that forwards to pause
    on trigger match (cropping logic lives only in pause).
    """

    def test_pause_accepts_stream_url(self):
        # Signature-level check — confirms the kwarg exists and is keyword-
        # only. Doing a real call needs a Playwright page; the integration
        # test covers that.
        import inspect

        sig = inspect.signature(Handoff.pause)
        assert "stream_url" in sig.parameters
        assert sig.parameters["stream_url"].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["stream_url"].default is None

    def test_guard_accepts_stream_url(self):
        import inspect

        sig = inspect.signature(Handoff.guard)
        assert "stream_url" in sig.parameters
        assert sig.parameters["stream_url"].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["stream_url"].default is None


class TestTimeoutKwargSignatures:
    """Lock the timeout kwargs on the current entry points.

    Both entry points expose `access_timeout` and `completion_timeout`
    overrides that default to None (= inherit ServerConfig).
    """

    def test_guard_uses_trigger_timeout(self):
        import inspect

        sig = inspect.signature(Handoff.guard)
        assert "trigger_timeout" in sig.parameters
        assert sig.parameters["trigger_timeout"].default == 30.0
        # `timeout` isn't on guard; it lives on the run() shim only.
        assert "timeout" not in sig.parameters

    def test_guard_has_timeout_overrides(self):
        import inspect

        sig = inspect.signature(Handoff.guard)
        for name in ("access_timeout", "completion_timeout"):
            assert name in sig.parameters, name
            assert sig.parameters[name].default is None

    def test_pause_has_timeout_overrides(self):
        import inspect

        sig = inspect.signature(Handoff.pause)
        for name in ("access_timeout", "completion_timeout"):
            assert name in sig.parameters, name
            assert sig.parameters[name].default is None


class TestWaitForCompletionShim:
    """wait_for_completion is a deprecated alias for pause."""

    def test_wait_for_completion_exists_as_shim(self):
        import inspect

        assert hasattr(Handoff, "wait_for_completion")
        # Same kwargs as pause except the second positional arg is
        # `on` (v0.6 name) rather than `until` (new pause parameter).
        old = inspect.signature(Handoff.wait_for_completion).parameters
        new = inspect.signature(Handoff.pause).parameters
        assert set(old.keys()) - {"on"} == set(new.keys()) - {"until"}
        assert "on" in old
        assert "until" in new

    def test_wait_for_completion_warns(self):
        import asyncio
        import contextlib

        h = Handoff()
        with pytest.warns(DeprecationWarning, match="wait_for_completion"):
            with contextlib.suppress(Exception):
                asyncio.run(
                    h.wait_for_completion(
                        object(), Detection.url(path_contains=["/x"])
                    )
                )


class TestRunShim:
    """run() is a deprecated alias for guard(). The shim also carries the
    older `timeout=` kwarg for callers still on the v0.6 signature."""

    def test_run_exists_as_shim(self):
        assert hasattr(Handoff, "run")

    def test_run_warns_and_forwards(self):
        import asyncio
        import contextlib

        h = Handoff()
        with pytest.warns(DeprecationWarning, match=r"Handoff\.run"):
            with contextlib.suppress(Exception):
                asyncio.run(h.run(object()))

    def test_run_timeout_kwarg_warns(self):
        # Both the method-name deprecation and the timeout-kwarg
        # deprecation fire; filter to the timeout-specific message.
        import asyncio
        import contextlib
        import warnings

        h = Handoff()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with contextlib.suppress(Exception):
                asyncio.run(h.run(object(), timeout=1.0))
        messages = [str(w.message) for w in caught]
        assert any("run(timeout" in m for m in messages), messages
