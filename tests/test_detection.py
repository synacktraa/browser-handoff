"""Tests for detection types."""

import asyncio

import pytest
from browser_handoff.detection import Detection
from browser_handoff.detection.combinators import AllDetection, AnyDetection, NotDetection
from browser_handoff.detection.content import ContentDetection
from browser_handoff.detection.element import ElementDetection
from browser_handoff.detection.llm import LLMDetection
from browser_handoff.detection.url import UrlDetection


class TestDetectionFactory:
    """Tests for the Detection factory class."""

    def test_content_creation(self):
        """Test creating content detection via factory."""
        detection = Detection.content(
            title_contains=["Test"],
            body_contains=["Hello"],
        )
        assert isinstance(detection, ContentDetection)
        assert detection.title_contains == ["Test"]
        assert detection.body_contains == ["Hello"]

    def test_url_creation(self):
        """Test creating URL detection via factory."""
        detection = Detection.url(
            host_equals=["localhost"],
            path_matches=["/callback"],
        )
        assert isinstance(detection, UrlDetection)
        assert detection.host_equals == ["localhost"]
        assert detection.path_matches == ["/callback"]

    def test_element_creation(self):
        """Test creating element detection via factory."""
        detection = Detection.element(
            present=["#login"],
            missing=[".error"],
        )
        assert isinstance(detection, ElementDetection)
        assert detection.present == ["#login"]
        assert detection.missing == [".error"]

    def test_all_combinator(self):
        """Test creating ALL combinator."""
        detection = Detection.all([
            Detection.content(title_contains=["Test"]),
            Detection.url(path_matches=["/test"]),
        ])
        assert isinstance(detection, AllDetection)
        assert len(detection.conditions) == 2

    def test_any_combinator(self):
        """Test creating ANY combinator."""
        detection = Detection.any([
            Detection.content(title_contains=["Test"]),
            Detection.url(path_matches=["/test"]),
        ])
        assert isinstance(detection, AnyDetection)
        assert len(detection.conditions) == 2

    def test_not_combinator(self):
        """Test creating NOT combinator."""
        detection = Detection.not_(
            Detection.element(present=[".error"])
        )
        assert isinstance(detection, NotDetection)
        assert detection.condition is not None

    def test_from_dict_content(self):
        """Test creating detection from dict - content type."""
        data = {
            "type": "content",
            "title_contains": ["Sign In"],
            "body_contains": ["please log in"],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, ContentDetection)
        assert detection.title_contains == ["Sign In"]

    def test_from_dict_url(self):
        """Test creating detection from dict - URL type."""
        data = {
            "type": "url",
            "host_equals": ["localhost"],
            "query_contains": ["code="],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, UrlDetection)
        assert detection.host_equals == ["localhost"]

    def test_from_dict_element(self):
        """Test creating detection from dict - element type."""
        data = {
            "type": "element",
            "present": ["input[type=password]"],
            "visible": [".consent-modal"],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, ElementDetection)
        assert detection.present == ["input[type=password]"]

    def test_from_dict_all_combinator(self):
        """Test creating detection from dict - all combinator."""
        data = {
            "type": "all",
            "conditions": [
                {"type": "content", "title_contains": ["Test"]},
                {"type": "url", "path_matches": ["/test"]},
            ],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, AllDetection)
        assert len(detection.conditions) == 2

    def test_from_dict_any_combinator(self):
        """Test creating detection from dict - any combinator."""
        data = {
            "type": "any",
            "conditions": [
                {"type": "element", "present": ["#success"]},
                {"type": "content", "body_contains": ["Welcome"]},
            ],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, AnyDetection)
        assert len(detection.conditions) == 2

    def test_from_dict_not_combinator(self):
        """Test creating detection from dict - not combinator."""
        data = {
            "type": "not",
            "condition": {"type": "element", "present": [".error"]},
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, NotDetection)
        assert isinstance(detection.condition, ElementDetection)

    def test_from_dict_unknown_type(self):
        """Test that unknown type raises ValueError."""
        data = {"type": "unknown"}
        with pytest.raises(ValueError, match="Unknown detection type"):
            Detection.from_dict(data)


class TestContentDetection:
    """Tests for ContentDetection."""

    def test_to_dict(self):
        """Test serialization to dict."""
        detection = ContentDetection(
            title_contains=["Test"],
            body_matches=[r"\d+"],
        )
        data = detection.to_dict()
        assert data["type"] == "content"
        assert data["title_contains"] == ["Test"]
        assert data["body_matches"] == [r"\d+"]

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "content",
            "title_contains": ["Hello"],
            "title_matches": [r"Test \d+"],
        }
        detection = ContentDetection.from_dict(data)
        assert detection.title_contains == ["Hello"]
        assert detection.title_matches == [r"Test \d+"]


class TestUrlDetection:
    """Tests for UrlDetection."""

    def test_to_dict(self):
        """Test serialization to dict."""
        detection = UrlDetection(
            scheme_equals="https",
            host_equals=["example.com"],
            path_contains=["/api/"],
        )
        data = detection.to_dict()
        assert data["type"] == "url"
        assert data["scheme_equals"] == "https"
        assert data["host_equals"] == ["example.com"]

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "url",
            "host_not_equals": ["blocked.com"],
            "query_contains": ["token="],
        }
        detection = UrlDetection.from_dict(data)
        assert detection.host_not_equals == ["blocked.com"]
        assert detection.query_contains == ["token="]


class TestElementDetection:
    """Tests for ElementDetection."""

    def test_to_dict(self):
        """Test serialization to dict."""
        detection = ElementDetection(
            present=["#login"],
            hidden=[".loader"],
        )
        data = detection.to_dict()
        assert data["type"] == "element"
        assert data["present"] == ["#login"]
        assert data["hidden"] == [".loader"]

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "element",
            "missing": [".error"],
            "visible": [".content"],
        }
        detection = ElementDetection.from_dict(data)
        assert detection.missing == [".error"]
        assert detection.visible == [".content"]


class TestCombinators:
    """Tests for combinator detections."""

    def test_all_to_dict(self):
        """Test AllDetection serialization."""
        detection = AllDetection(conditions=[
            ContentDetection(title_contains=["Test"]),
            UrlDetection(path_matches=["/test"]),
        ])
        data = detection.to_dict()
        assert data["type"] == "all"
        assert len(data["conditions"]) == 2
        assert data["conditions"][0]["type"] == "content"
        assert data["conditions"][1]["type"] == "url"

    def test_any_to_dict(self):
        """Test AnyDetection serialization."""
        detection = AnyDetection(conditions=[
            ElementDetection(present=["#a"]),
            ElementDetection(present=["#b"]),
        ])
        data = detection.to_dict()
        assert data["type"] == "any"
        assert len(data["conditions"]) == 2

    def test_not_to_dict(self):
        """Test NotDetection serialization."""
        detection = NotDetection(
            condition=ElementDetection(present=[".error"])
        )
        data = detection.to_dict()
        assert data["type"] == "not"
        assert data["condition"]["type"] == "element"

    def test_nested_combinators(self):
        """Test nested combinator serialization/deserialization."""
        detection = Detection.all([
            Detection.any([
                Detection.content(title_contains=["A"]),
                Detection.content(title_contains=["B"]),
            ]),
            Detection.not_(Detection.element(present=[".error"])),
        ])

        data = detection.to_dict()
        restored = Detection.from_dict(data)

        assert isinstance(restored, AllDetection)
        assert len(restored.conditions) == 2
        assert isinstance(restored.conditions[0], AnyDetection)
        assert isinstance(restored.conditions[1], NotDetection)


# ---- LLM detection: activity-debounced watch loop -----------------------
#
# These cover the cost-control logic added to LLMDetection.register_listeners:
# instead of polling the vision model every N seconds, it watches for human
# input + DOM change + navigation, and runs ONE check once activity settles
# (idle_seconds), with an optional safety-net poll (max_interval). The pure
# decision lives in _should_check so it can be tested without a browser or an
# LLM call; the loop wiring is exercised against a fake page.


class TestLLMShouldCheck:
    """Pure debounce decision — no browser, no model call."""

    def test_no_activity_never_checks(self):
        # Nothing has happened yet → never spend a check.
        assert (
            LLMDetection._should_check(
                now=100.0,
                last_activity=None,
                last_check=0.0,
                last_check_activity=None,
                idle_seconds=2.0,
                max_interval=30.0,
            )
            is False
        )

    def test_recent_activity_not_yet_idle(self):
        # Activity 1s ago, idle window 2s → still settling, don't check.
        assert (
            LLMDetection._should_check(
                now=101.0,
                last_activity=100.0,
                last_check=0.0,
                last_check_activity=None,
                idle_seconds=2.0,
                max_interval=0.0,
            )
            is False
        )

    def test_settled_activity_triggers(self):
        # Idle window elapsed since the last activity → one check.
        assert (
            LLMDetection._should_check(
                now=102.5,
                last_activity=100.0,
                last_check=0.0,
                last_check_activity=None,
                idle_seconds=2.0,
                max_interval=0.0,
            )
            is True
        )

    def test_already_checked_for_this_burst(self):
        # We already checked covering this exact activity → don't re-check.
        assert (
            LLMDetection._should_check(
                now=105.0,
                last_activity=100.0,
                last_check=102.5,
                last_check_activity=100.0,
                idle_seconds=2.0,
                max_interval=0.0,
            )
            is False
        )

    def test_new_activity_after_check_retriggers(self):
        # Fresh activity (later than the last checked one) settles → check.
        assert (
            LLMDetection._should_check(
                now=110.0,
                last_activity=107.0,
                last_check=102.5,
                last_check_activity=100.0,
                idle_seconds=2.0,
                max_interval=0.0,
            )
            is True
        )

    def test_safety_net_fires_without_new_activity(self):
        # No new activity since last check, but max_interval elapsed → poll
        # once anyway, to catch changes we can't observe via DOM/input.
        assert (
            LLMDetection._should_check(
                now=140.0,
                last_activity=100.0,
                last_check=105.0,
                last_check_activity=100.0,
                idle_seconds=2.0,
                max_interval=30.0,
            )
            is True
        )

    def test_safety_net_disabled(self):
        # max_interval=0 disables the safety-net poll entirely.
        assert (
            LLMDetection._should_check(
                now=140.0,
                last_activity=100.0,
                last_check=105.0,
                last_check_activity=100.0,
                idle_seconds=2.0,
                max_interval=0.0,
            )
            is False
        )

    def test_safety_net_requires_prior_activity(self):
        # Even with max_interval elapsed, never check if nothing ever happened.
        assert (
            LLMDetection._should_check(
                now=140.0,
                last_activity=None,
                last_check=105.0,
                last_check_activity=None,
                idle_seconds=2.0,
                max_interval=30.0,
            )
            is False
        )


class TestLLMSerialization:
    """to_dict / from_dict carry the debounce knobs."""

    def test_to_dict_includes_debounce_knobs(self):
        data = LLMDetection(
            condition="login form visible", idle_seconds=1.5, max_interval=10.0
        ).to_dict()
        assert data["type"] == "llm"
        assert data["idle_seconds"] == 1.5
        assert data["max_interval"] == 10.0

    def test_from_dict_round_trip(self):
        src = LLMDetection(
            model="openai/gpt-4o",
            condition="done",
            idle_seconds=1.0,
            max_interval=5.0,
        )
        back = LLMDetection.from_dict(src.to_dict())
        assert back.model == "openai/gpt-4o"
        assert back.condition == "done"
        assert back.idle_seconds == 1.0
        assert back.max_interval == 5.0

    def test_from_dict_defaults(self):
        back = LLMDetection.from_dict({"type": "llm", "condition": "x"})
        assert back.idle_seconds == 2.0
        assert back.max_interval == 30.0


class _FakeActivityPage:
    """Minimal Playwright-Page stand-in for the debounce loop.

    `activity_ms` mimics window.__bhLastActivity (the JS-side timestamp the
    injected listeners write to). Bump it to simulate the operator interacting
    or the DOM mutating; the loop polls it via evaluate().
    """

    def __init__(self):
        self.activity_ms = 0
        self.init_scripts: list[str] = []
        self.listeners: dict[str, list] = {}

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def evaluate(self, script, *args):
        # The setup script installs listeners (contains addEventListener) and
        # returns nothing; the activity-read script just returns the stamp.
        if "addEventListener" in script:
            return None
        return self.activity_ms

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.listeners.get(event, []).remove(handler)


class TestLLMWatchLoop:
    """Loop wiring against a fake page (no browser, no model)."""

    async def test_loop_debounces_then_stops_on_cleanup(self):
        det = LLMDetection(condition="x", idle_seconds=0.15, max_interval=0.0)
        det._poll_interval = 0.02
        page = _FakeActivityPage()
        calls: list = []

        async def cb(d):
            calls.append(d)

        cleanup = det.register_listeners(page, cb)
        try:
            # No activity → no checks.
            await asyncio.sleep(0.2)
            assert calls == []

            # Operator interacts → exactly one check once the idle window passes.
            page.activity_ms = 1_000
            await asyncio.sleep(0.06)
            assert calls == []  # still within the idle window
            await asyncio.sleep(0.22)
            assert len(calls) == 1

            # No new activity → debounced, no further checks.
            await asyncio.sleep(0.25)
            assert len(calls) == 1

            # Fresh activity → another single check after it settles.
            page.activity_ms = 2_000
            await asyncio.sleep(0.3)
            assert len(calls) == 2
        finally:
            cleanup()

        # Loop stopped: no checks after cleanup, even with new activity.
        snapshot = len(calls)
        page.activity_ms = 3_000
        await asyncio.sleep(0.3)
        assert len(calls) == snapshot

    async def test_navigation_counts_as_activity(self):
        det = LLMDetection(condition="x", idle_seconds=0.15, max_interval=0.0)
        det._poll_interval = 0.02
        page = _FakeActivityPage()
        calls: list = []

        async def cb(d):
            calls.append(d)

        cleanup = det.register_listeners(page, cb)
        try:
            await asyncio.sleep(0.05)
            # Fire the framenavigated listener the loop registered.
            for handler in list(page.listeners.get("framenavigated", [])):
                handler()
            await asyncio.sleep(0.25)
            assert len(calls) == 1
        finally:
            cleanup()
