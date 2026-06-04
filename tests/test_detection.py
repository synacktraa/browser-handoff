"""Tests for detection types."""

import asyncio
from types import SimpleNamespace

import pytest
from browser_handoff.detection import Detection
from browser_handoff.detection.combinators import AllDetection, AnyDetection, NotDetection
from browser_handoff.detection.content import ContentDetection
from browser_handoff.detection.element import ElementDetection
from browser_handoff.detection.llm import LLMDetection
from browser_handoff.detection.url import UrlDetection
from browser_handoff.server.operator_activity import OperatorActivity


def _fake_session(operator_activity: OperatorActivity | None = None, reason: str = "") -> SimpleNamespace:
    """Minimal session-like stub for bind() tests.

    BaseDetection.bind() takes a HandoffSession, but constructing a real one
    means dummies for session_id / page / context / cdp / etc. Detections
    only read `.operator_activity` and `.reason`, so SimpleNamespace duck-
    types cleanly and keeps the tests focused.
    """
    return SimpleNamespace(
        operator_activity=operator_activity if operator_activity is not None else OperatorActivity(),
        reason=reason,
    )


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


# ---- Verbose reason strings ---------------------------------------------
#
# Each leaf detection's success reason names the user-configured clauses
# that fired (not "matches all conditions" / "All element conditions met"
# generic stubs), and the AND combinator joins child reasons as a bulleted
# list with nested-AND flattening. The reason flows through to notifiers
# and the stream-viewer breadcrumb, so coverage stays on the strings.


class _StubElement:
    def __init__(self, visible: bool = True) -> None:
        self._visible = visible

    async def is_visible(self) -> bool:
        return self._visible


class _StubPage:
    """Minimal Playwright Page stand-in for reason-string tests — no browser."""

    def __init__(
        self,
        *,
        url: str = "http://example.com/",
        title: str = "",
        body: str = "",
        selectors: dict[str, _StubElement] | None = None,
    ) -> None:
        self.url = url
        self._title = title
        self._body = body
        self._selectors = selectors or {}

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._body

    async def query_selector(self, selector: str):
        return self._selectors.get(selector)


class TestVerboseReasons:
    """Leaf detections name the configured clauses that fired."""

    async def test_url_reason_names_clauses(self):
        det = UrlDetection(
            host_equals=["example.com"],
            path_contains=["/login"],
        )
        page = _StubPage(url="https://example.com/login?return=/dashboard")
        result = await det.check(page)
        assert result.matched
        assert "host_equals matched 'example.com'" in result.reason
        assert "path_contains matched '/login'" in result.reason

    async def test_url_reason_no_conditions(self):
        det = UrlDetection()
        page = _StubPage(url="https://example.com/")
        result = await det.check(page)
        assert result.matched
        assert "no conditions configured" in result.reason

    async def test_element_reason_lists_selectors(self):
        det = ElementDetection(
            present=["button.submit"],
            visible=[".banner"],
        )
        page = _StubPage(
            selectors={
                "button.submit": _StubElement(visible=True),
                ".banner": _StubElement(visible=True),
            }
        )
        result = await det.check(page)
        assert result.matched
        assert "present=['button.submit']" in result.reason
        assert "visible=['.banner']" in result.reason

    async def test_content_reason_names_clause(self):
        det = ContentDetection(title_contains=["Sign in"])
        page = _StubPage(title="Sign in to Claude")
        result = await det.check(page)
        assert result.matched
        assert "title_contains 'Sign in'" in result.reason


class TestAndReasonAggregation:
    """AND combinator joins children as a bulleted list and flattens nested ANDs."""

    async def test_joined_with_bullets(self):
        url = UrlDetection(path_contains=["/login"])
        element = ElementDetection(present=["button.submit"])
        page = _StubPage(
            url="https://example.com/login",
            selectors={"button.submit": _StubElement(visible=True)},
        )
        det = AllDetection(conditions=[url, element])
        result = await det.check(page)
        assert result.matched
        assert result.reason.startswith("Matched conditions:\n• ")
        bullets = [
            line for line in result.reason.split("\n") if line.startswith("• ")
        ]
        assert len(bullets) == 2
        assert "path_contains matched '/login'" in result.reason
        assert "present=['button.submit']" in result.reason

    async def test_nested_and_flattens(self):
        url = UrlDetection(path_contains=["/login"])
        element = ElementDetection(present=["#submit"])
        content = ContentDetection(title_contains=["Sign in"])
        inner = AllDetection(conditions=[element, content])
        outer = AllDetection(conditions=[url, inner])
        page = _StubPage(
            url="https://example.com/login",
            title="Sign in to Claude",
            selectors={"#submit": _StubElement(visible=True)},
        )
        result = await outer.check(page)
        assert result.matched
        # Three leaf bullets, no nested "Matched conditions:" header.
        bullets = [
            line for line in result.reason.split("\n") if line.startswith("• ")
        ]
        assert len(bullets) == 3
        assert result.reason.count("Matched conditions:") == 1


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
        assert back.idle_seconds == 3.0
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


# ---- OperatorActivity primitive -----------------------------------------
#
# Owned by HandoffSession, mutated by the streaming server on each routed
# operator event, read by LLMDetection.bind() to gate vision calls on
# operator presence. These pin the primitive itself; the wiring through
# the server lives in integration tests.


class TestOperatorActivity:
    """OperatorActivity bump / wait_for_first_interaction semantics."""

    def test_starts_idle(self):
        a = OperatorActivity()
        assert a.last_activity is None
        assert a.has_ever_interacted is False

    def test_bump_records_monotonic_time(self):
        a = OperatorActivity()
        a.bump()
        assert a.last_activity is not None
        assert a.has_ever_interacted is True

    async def test_wait_for_first_interaction_blocks_until_bump(self):
        a = OperatorActivity()
        wait_task = asyncio.create_task(a.wait_for_first_interaction())
        # Yielding gives the task a chance to advance; it should still be
        # pending because no bump has happened.
        await asyncio.sleep(0.02)
        assert not wait_task.done()
        a.bump()
        await asyncio.wait_for(wait_task, timeout=0.5)

    async def test_wait_for_first_interaction_returns_immediately_after_bump(self):
        # Gate stays open for the rest of the handoff so late waiters don't
        # block — load-bearing for watch-loop restart scenarios.
        a = OperatorActivity()
        a.bump()
        await asyncio.wait_for(a.wait_for_first_interaction(), timeout=0.1)


# ---- LLMDetection: operator-activity-gated path (bound) -----------------
#
# When bind() supplies an OperatorActivity, the loop must (a) make zero
# vision calls until the operator interacts at all, and (b) after that, fire
# exactly one debounced check per idle settle — same _should_check logic as
# the page-activity path, just a different timestamp source.


class TestLLMOperatorGatedWatchLoop:
    """LLMDetection watch loop bound to an OperatorActivity."""

    async def test_no_calls_until_first_operator_interaction(self):
        det = LLMDetection(condition="x", idle_seconds=0.15, max_interval=0.0)
        det._poll_interval = 0.02
        activity = OperatorActivity()
        det.bind(session=_fake_session(operator_activity=activity))
        calls: list = []

        async def cb(d):
            calls.append(d)

        # page is not consulted in the bound path — pass a sentinel that
        # would explode if any method got called on it.
        cleanup = det.register_listeners(page=None, callback=cb)
        try:
            # Long enough that the page-activity path's safety net would
            # have fired at least once. Nothing should happen here.
            await asyncio.sleep(0.4)
            assert calls == []
        finally:
            cleanup()

    async def test_one_debounced_check_per_idle_settle(self):
        det = LLMDetection(condition="x", idle_seconds=0.15, max_interval=0.0)
        det._poll_interval = 0.02
        activity = OperatorActivity()
        det.bind(session=_fake_session(operator_activity=activity))
        calls: list = []

        async def cb(d):
            calls.append(d)

        cleanup = det.register_listeners(page=None, callback=cb)
        try:
            # First interaction opens the gate and starts the idle window.
            activity.bump()
            await asyncio.sleep(0.06)
            assert calls == []  # still inside idle_seconds
            await asyncio.sleep(0.22)
            assert len(calls) == 1

            # No new activity → no second check.
            await asyncio.sleep(0.25)
            assert len(calls) == 1

            # Fresh interaction → one more after idle_seconds settles.
            activity.bump()
            await asyncio.sleep(0.3)
            assert len(calls) == 2
        finally:
            cleanup()

    async def test_cleanup_before_first_interaction_does_not_hang(self):
        # The "wait for first interaction" select must lose cleanly to
        # cleanup, otherwise the watch task would leak forever when the
        # operator never shows up.
        det = LLMDetection(condition="x", idle_seconds=0.15)
        det._poll_interval = 0.02
        activity = OperatorActivity()
        det.bind(session=_fake_session(operator_activity=activity))

        async def cb(_d):
            pass

        cleanup = det.register_listeners(page=None, callback=cb)
        await asyncio.sleep(0.05)  # task is parked on wait_for_first_interaction
        cleanup()
        # Give the cancel a tick to propagate. If the task were leaking we'd
        # see a "Task was destroyed but it is pending" warning, but the
        # functional check is just that this point is reached without a hang.
        await asyncio.sleep(0.05)


# ---- Combinator bind() propagation --------------------------------------


class _RecordingDetection(LLMDetection):
    """Tracks bind() calls — used to verify combinator propagation.

    Subclassing LLMDetection (rather than BaseDetection) keeps the inherited
    bind() override under test; combinators must walk into it.
    """

    def __init__(self):
        super().__init__(condition="x")
        self.bound_to: list = []

    def bind(self, *, session=None):
        self.bound_to.append(session)
        super().bind(session=session)


class TestCombinatorBindPropagation:
    """AllDetection / AnyDetection / NotDetection propagate bind() to children."""

    def test_all_propagates_to_each_child(self):
        a, b = _RecordingDetection(), _RecordingDetection()
        all_det = AllDetection(conditions=[a, b])
        session = _fake_session()
        all_det.bind(session=session)
        assert a.bound_to == [session]
        assert b.bound_to == [session]

    def test_any_propagates_to_each_child(self):
        a, b = _RecordingDetection(), _RecordingDetection()
        any_det = AnyDetection(conditions=[a, b])
        session = _fake_session()
        any_det.bind(session=session)
        assert a.bound_to == [session]
        assert b.bound_to == [session]

    def test_not_propagates_to_wrapped(self):
        inner = _RecordingDetection()
        not_det = NotDetection(condition=inner)
        session = _fake_session()
        not_det.bind(session=session)
        assert inner.bound_to == [session]

    def test_not_with_no_condition_is_safe(self):
        # NotDetection allows condition=None; bind() must not blow up.
        NotDetection(condition=None).bind(session=_fake_session())


# ---- Element detection: poll-based watch loop (no expose_function) -------
#
# ElementDetection used to push mutations to Python via expose_function (a
# detectable Runtime.addBinding) plus two fixed enumerable window globals. It
# now uses the same stealthy poll model as LLMDetection: a MutationObserver
# stamps a per-session, non-enumerable window var, and Python polls it — no
# binding, nothing enumerable, no shared dispatcher. These pin the new loop
# wiring against a fake page; the browser-side behavior + stealth live in
# tests/integration/test_element_detection.py.


class _FakeObserverPage:
    """Playwright-Page stand-in for the element poll loop.

    `activity_ms` mimics the hidden window stamp the injected MutationObserver
    writes. `exposed` records any expose_function call — it must stay empty,
    since dropping that binding is the whole point of the rewrite.
    """

    def __init__(self):
        self.activity_ms = 0
        self.init_scripts: list[str] = []
        self.exposed: list[str] = []
        self.listeners: dict[str, list] = {}

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def evaluate(self, script, *args):
        # Setup script installs the observer (returns nothing); the read script
        # just returns the stamp.
        if "MutationObserver" in script:
            return None
        return self.activity_ms

    async def expose_function(self, name, fn):  # pragma: no cover - must not run
        self.exposed.append(name)

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.listeners.get(event, []).remove(handler)


class TestElementWatchLoop:
    """Loop wiring against a fake page (no browser)."""

    async def test_fires_on_stamp_advance_and_never_binds(self):
        det = ElementDetection(present=["x"])
        det._poll_interval = 0.02
        page = _FakeObserverPage()
        calls: list = []

        async def cb(d):
            calls.append(d)

        cleanup = det.register_listeners(page, cb)
        try:
            # No mutation yet → no fire.
            await asyncio.sleep(0.06)
            assert calls == []

            # Mutation stamps the var → one fire on the next poll.
            page.activity_ms = 1_000
            await asyncio.sleep(0.08)
            assert len(calls) == 1
            # No new mutation → no repeat fire.
            await asyncio.sleep(0.08)
            assert len(calls) == 1

            # Navigation resets the var to 0 — that alone must NOT fire.
            page.activity_ms = 0
            await asyncio.sleep(0.06)
            assert len(calls) == 1

            # A post-nav mutation fires again.
            page.activity_ms = 2_000
            await asyncio.sleep(0.08)
            assert len(calls) == 2
        finally:
            cleanup()

        # The whole point: no expose_function / addBinding was ever used.
        assert page.exposed == []

        # Loop stopped after cleanup.
        snapshot = len(calls)
        page.activity_ms = 3_000
        await asyncio.sleep(0.08)
        assert len(calls) == snapshot

    async def test_navigation_triggers_check(self):
        det = ElementDetection(present=["x"])
        det._poll_interval = 0.02
        page = _FakeObserverPage()
        calls: list = []

        async def cb(d):
            calls.append(d)

        cleanup = det.register_listeners(page, cb)
        try:
            await asyncio.sleep(0.04)
            for handler in list(page.listeners.get("framenavigated", [])):
                handler()
            await asyncio.sleep(0.08)
            assert len(calls) >= 1
        finally:
            cleanup()

    async def test_independent_subscriptions(self):
        """Two detections on one page fire independently; cleaning one leaves
        the other running (no shared dispatcher to break)."""
        page = _FakeObserverPage()
        calls_a: list = []
        calls_b: list = []

        async def cb_a(d):
            calls_a.append(d)

        async def cb_b(d):
            calls_b.append(d)

        det_a = ElementDetection(present=["a"])
        det_b = ElementDetection(present=["b"])
        det_a._poll_interval = det_b._poll_interval = 0.02
        cleanup_a = det_a.register_listeners(page, cb_a)
        cleanup_b = det_b.register_listeners(page, cb_b)
        try:
            page.activity_ms = 1_000
            await asyncio.sleep(0.08)
            assert len(calls_a) == 1 and len(calls_b) == 1

            cleanup_a()
            page.activity_ms = 2_000
            await asyncio.sleep(0.08)
            assert len(calls_a) == 1, "cleaned-up subscription still fired"
            assert len(calls_b) == 2, "remaining subscription stopped firing"
        finally:
            cleanup_b()
