"""Tests for server configuration and the streaming server."""

import asyncio
import contextlib
import time

import pytest
from starlette.middleware.cors import CORSMiddleware

from browser_handoff.server import ServerConfig
from browser_handoff.server.session import HandoffSession
from browser_handoff.server.streaming import StreamingServer


def _bare_session(**overrides) -> HandoffSession:
    """A HandoffSession with placeholder page/context/cdp.

    _spawn_tracked only touches session.background_tasks, so the browser
    objects can be inert stand-ins. Pass overrides to set additional
    fields (e.g. stream_url for passthrough-mode tests)."""
    fields = dict(
        session_id="test",
        page=object(),
        context=object(),
        cdp=object(),
        reason="test",
    )
    fields.update(overrides)
    return HandoffSession(**fields)


class TestServerConfig:
    """Tests for ServerConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = ServerConfig()
        assert config.host == "127.0.0.1"
        assert config.port == 8080
        assert config.public_base is None
        assert config.access_timeout == 600.0
        assert config.completion_timeout == 1800.0
        assert config.jpeg_quality == 75
        assert config.every_nth_frame == 1

    def test_custom_values(self):
        """Test custom configuration values."""
        config = ServerConfig(
            host="localhost",
            port=3000,
            public_base="https://proxy.example.com",
            access_timeout=120.0,
            completion_timeout=300.0,
        )
        assert config.host == "localhost"
        assert config.port == 3000
        assert config.public_base == "https://proxy.example.com"
        assert config.access_timeout == 120.0
        assert config.completion_timeout == 300.0

    def test_timeout_layers_accept_none(self):
        # None at the config layer means truly no bound at that layer.
        config = ServerConfig(access_timeout=None, completion_timeout=None)
        assert config.access_timeout is None
        assert config.completion_timeout is None

    def test_old_session_timeout_rejected(self):
        # Clean break — the deprecated knob is gone; the dataclass
        # raises immediately so misuse fails at construction.
        with pytest.raises(TypeError):
            ServerConfig(session_timeout=300.0)

    def test_get_base_url_with_public_base(self):
        """Test get_base_url with public_base set."""
        config = ServerConfig(
            host="localhost",
            port=8080,
            public_base="https://proxy.example.com/",
        )
        assert config.get_base_url() == "https://proxy.example.com"

    def test_get_base_url_without_public_base(self):
        """Test get_base_url without public_base."""
        config = ServerConfig(host="localhost", port=3000)
        assert config.get_base_url() == "http://localhost:3000"

    def test_get_base_url_rewrites_wildcard(self):
        """Wildcard binds (0.0.0.0, ::) get rewritten to localhost in URLs."""
        for wildcard in ("0.0.0.0", "::", ""):
            config = ServerConfig(host=wildcard, port=8080)
            assert config.get_base_url() == "http://localhost:8080"

    def test_to_dict(self):
        """Test serialization to dict."""
        config = ServerConfig(
            host="0.0.0.0",
            port=8080,
            public_base="https://example.com",
            access_timeout=120.0,
            completion_timeout=240.0,
        )
        data = config.to_dict()
        assert data == {
            "host": "0.0.0.0",
            "port": 8080,
            "public_base": "https://example.com",
            "access_timeout": 120.0,
            "completion_timeout": 240.0,
            "jpeg_quality": 75,
            "every_nth_frame": 1,
        }

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "host": "127.0.0.1",
            "port": 9000,
            "public_base": "https://proxy.test.com",
            "access_timeout": 30.0,
            "completion_timeout": 60.0,
        }
        config = ServerConfig.from_dict(data)
        assert config.host == "127.0.0.1"
        assert config.port == 9000
        assert config.public_base == "https://proxy.test.com"
        assert config.access_timeout == 30.0
        assert config.completion_timeout == 60.0

    def test_from_dict_defaults(self):
        """Test from_dict with missing values uses defaults."""
        config = ServerConfig.from_dict({})
        assert config.host == "127.0.0.1"
        assert config.port == 8080
        assert config.public_base is None
        assert config.access_timeout == 600.0
        assert config.completion_timeout == 1800.0

    def test_from_dict_partial(self):
        """Test from_dict with partial values."""
        config = ServerConfig.from_dict({"port": 5000})
        assert config.host == "127.0.0.1"
        assert config.port == 5000
        assert config.public_base is None


class TestCORSConfig:
    """The CORS middleware must not pair wildcard origins with credentials."""

    def _cors_kwargs(self) -> dict:
        app = StreamingServer().app
        for mw in app.user_middleware:
            if mw.cls is CORSMiddleware:
                return mw.kwargs
        raise AssertionError("CORSMiddleware not configured")

    def test_credentials_disabled_with_wildcard_origin(self):
        # Browsers reject "*" + credentials, and Starlette disallows the combo;
        # the stream is gated by the session id, not cookies, so credentials
        # must stay off.
        kwargs = self._cors_kwargs()
        assert kwargs["allow_origins"] == ["*"]
        assert kwargs["allow_credentials"] is False


class TestSpawnTracked:
    """Fire-and-forget tasks are held by a strong ref until they finish.

    The event loop keeps only weak references, so the ack/publish tasks must
    live in session.background_tasks (else they can be GC'd mid-flight — a
    dropped screencast ack stalls capture) and clear themselves on completion.
    """

    async def test_held_while_pending_then_discarded_on_completion(self):
        session = _bare_session()
        gate = asyncio.Event()

        async def work() -> None:
            await gate.wait()

        task = StreamingServer._spawn_tracked(session, work())
        assert task in session.background_tasks, "task must be held while running"

        gate.set()
        await task
        await asyncio.sleep(0)  # let the done-callback run
        assert session.background_tasks == set(), "completed task must self-remove"

    async def test_discarded_on_cancellation(self):
        # The teardown paths cancel these tasks; cancellation must still clear
        # the set (the done-callback fires on cancel too).
        session = _bare_session()
        started = asyncio.Event()

        async def work() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = StreamingServer._spawn_tracked(session, work())
        await started.wait()
        assert task in session.background_tasks

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert session.background_tasks == set(), "cancelled task must self-remove"


class TestAccessToken:
    """The stream URL is gated by a strong, expiring capability token."""

    def _register(self, server: StreamingServer, expires_at: float | None):
        """Insert a session into the server without a real browser/CDP.

        register_session needs a live page; these tests only exercise token
        resolution and URL building, so wire the maps up by hand instead.
        """
        session = _bare_session()
        session.expires_at = expires_at
        server.sessions[session.session_id] = session
        server._token_to_session[session.access_token] = session.session_id
        return session

    def test_token_is_strong_and_decoupled_from_session_id(self):
        session = _bare_session()
        # URL-safe and long enough to be unguessable (token_urlsafe(32) ≈ 43).
        assert len(session.access_token) >= 32
        assert session.access_token != session.session_id

    def test_resolve_valid_token(self):
        server = StreamingServer()
        session = self._register(server, expires_at=time.time() + 60)
        assert server._resolve_token(session.access_token) is session

    def test_resolve_unknown_or_empty_token(self):
        server = StreamingServer()
        self._register(server, expires_at=time.time() + 60)
        assert server._resolve_token("nope") is None
        assert server._resolve_token(None) is None
        assert server._resolve_token("") is None

    def test_resolve_expired_token(self):
        server = StreamingServer()
        session = self._register(server, expires_at=time.time() - 1)  # already past
        assert server._resolve_token(session.access_token) is None

    def test_operator_url_carries_token_not_session_id(self):
        server = StreamingServer()
        session = self._register(server, expires_at=time.time() + 60)
        url = server.get_operator_url(session.session_id)
        assert f"?t={session.access_token}" in url
        assert "?session=" not in url  # the id is no longer the URL gate

    def test_get_stream_url_is_deprecated_alias(self):
        import warnings

        server = StreamingServer()
        session = self._register(server, expires_at=time.time() + 60)
        canonical = server.get_operator_url(session.session_id)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            legacy = server.get_stream_url(session.session_id)
        assert legacy == canonical
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)


class TestPassthroughSession:
    """HandoffSession.is_passthrough is derived from stream_url.

    Single source of truth so the two can't disagree. Passthrough state
    flips solely on whether a substrate-served stream URL was provided.
    """

    def test_is_passthrough_false_when_stream_url_absent(self):
        session = _bare_session()
        assert session.stream_url is None
        assert session.is_passthrough is False

    def test_is_passthrough_true_when_stream_url_set(self):
        session = _bare_session(stream_url="https://substrate.example/viewer?t=abc")
        assert session.is_passthrough is True

    def test_crop_metrics_optional(self):
        session = _bare_session(stream_url="https://substrate/viewer")
        assert session.crop_metrics is None

    def test_crop_metrics_carried_when_provided(self):
        metrics = {
            "screen_w": 1920, "screen_h": 1080,
            "page_x": 0, "page_y": 87,
            "page_w": 1920, "page_h": 993,
        }
        session = _bare_session(
            stream_url="https://substrate/viewer",
            crop_metrics=metrics,
        )
        assert session.crop_metrics == metrics


class TestCaptureFramesPassthroughGuard:
    """The CDP screencast pump must early-return on passthrough sessions.

    Belt-and-suspenders: register_session skips scheduling the task in the
    first place when stream_url is set, but a stray caller invoking
    _capture_frames directly on a passthrough session must not start a
    screencast on a page whose viewer is the substrate's (not ours).
    """

    async def test_capture_frames_returns_immediately_for_passthrough(self):
        server = StreamingServer()
        session = _bare_session(stream_url="https://substrate/viewer")

        # If the guard works, the coroutine completes without touching
        # session.cdp at all (the placeholder cdp=object() would raise on
        # any attribute access). Wrap in asyncio.wait_for so a missing
        # guard hangs the test instead of looping forever.
        await asyncio.wait_for(server._capture_frames(session), timeout=1.0)


class TestTemplateSelection:
    """`_get_html_client` picks the right template based on session mode."""

    def _register(self, server: StreamingServer, **overrides):
        session = _bare_session(**overrides)
        session.expires_at = time.time() + 60
        server.sessions[session.session_id] = session
        server._token_to_session[session.access_token] = session.session_id
        return session

    def test_streaming_session_renders_intervention_template(self):
        server = StreamingServer()
        self._register(server)
        html = server._get_html_client("test", "please log in")
        # intervention.html ships the streaming-mode features that the
        # proxy template intentionally omits.
        assert "Browser Handoff" in html
        assert "please log in" in html
        assert "stream-container" in html  # streaming-only element id

    def test_passthrough_session_renders_proxy_template(self):
        server = StreamingServer()
        crop = {
            "screen_w": 1920, "screen_h": 1080,
            "page_x": 0, "page_y": 87,
            "page_w": 1920, "page_h": 993,
        }
        self._register(
            server,
            stream_url="https://substrate.example/viewer?t=xyz",
            crop_metrics=crop,
        )
        html = server._get_html_client("test", "please sign in")
        assert "please sign in" in html
        # Proxy-only markers: the substrate iframe and the fallback
        # screenshot used when the bh session ends without completion
        # (substrate's WebRTC stream would otherwise keep running in the
        # iframe). Neither exists in intervention.html.
        assert "substrate-iframe" in html
        assert "fallback-screenshot" in html
        # Crop metrics threaded into the CSS via Jinja.
        assert "1920" in html and "993" in html

    def test_passthrough_template_falls_back_without_crop_metrics(self):
        # Degenerate crop (e.g. headless mocks returned zero dims) must
        # still render a usable wrapper — just without the iframe crop.
        server = StreamingServer()
        self._register(
            server,
            stream_url="https://substrate.example/viewer",
            crop_metrics=None,
        )
        html = server._get_html_client("test", "intervene please")
        assert "substrate-iframe" in html
        # No crop math should appear when metrics are None.
        assert "calc(-100% *" not in html


class TestPassthroughNotifications:
    """Status WS event shape for passthrough lifecycle events."""

    async def test_notify_task_expired_sends_event_to_all_websockets(self):
        server = StreamingServer()
        session = _bare_session(stream_url="https://substrate/viewer")
        server.sessions[session.session_id] = session

        sent: list[dict] = []

        class FakeWS:
            async def send_json(self, payload):
                sent.append(payload)

        session.websockets.extend([FakeWS(), FakeWS()])
        await server.notify_task_expired(session.session_id)

        assert sent == [{"type": "task_expired"}, {"type": "task_expired"}]

    async def test_notify_task_expired_unknown_session_is_noop(self):
        # Race: the session unregistered between detect-timeout and notify.
        # Must not raise.
        server = StreamingServer()
        await server.notify_task_expired("nope")  # silent

    async def test_notify_task_expired_survives_send_failure(self):
        # A disconnected WS must not abort the broadcast to the others.
        server = StreamingServer()
        session = _bare_session(stream_url="https://substrate/viewer")
        server.sessions[session.session_id] = session

        delivered: list[dict] = []

        class BrokenWS:
            async def send_json(self, payload):
                raise RuntimeError("disconnected")

        class GoodWS:
            async def send_json(self, payload):
                delivered.append(payload)

        session.websockets.extend([BrokenWS(), GoodWS()])
        await server.notify_task_expired(session.session_id)
        # The good WS still got the event despite the broken one raising.
        assert delivered == [{"type": "task_expired"}]

    async def test_notify_task_cancelled_sends_distinct_event(self):
        # The server still sends a structurally distinct task_cancelled
        # event so callers / future client variants can act on it; the
        # current operator wrapper collapses it into the same "Session
        # ended" card as a raw connection drop because the cause isn't
        # actionable for an operator.
        server = StreamingServer()
        session = _bare_session(stream_url="https://substrate/viewer")
        server.sessions[session.session_id] = session

        sent: list[dict] = []

        class FakeWS:
            async def send_json(self, payload):
                sent.append(payload)

        session.websockets.append(FakeWS())
        await server.notify_task_cancelled(session.session_id)
        assert sent == [{"type": "task_cancelled"}]


class TestLLMActivitySetupJS:
    """The stealth in-page activity JS used by LLMDetection's unified watcher.

    The same setup script is the only operator-activity signal in both
    streaming and passthrough modes after the watcher refactor — owned by
    LLMDetection, mode-agnostic. These pin the JS shape; functional
    in-browser behavior is in tests/integration/test_llm_activity.py.
    """

    def test_setup_js_uses_non_enumerable_property(self):
        # Stealth claim: site JS that walks window must not see the stamp.
        from browser_handoff.detection.llm import _activity_setup_js

        js = _activity_setup_js("__bh_abc123")
        assert "Object.defineProperty(window," in js
        assert "enumerable: false" in js

    def test_setup_js_attaches_only_input_listeners(self):
        # Capture+passive input listeners — same stealth pattern that used
        # to live in _PassthroughActivityWatcher. No MutationObserver:
        # page-driven DOM mutations (carousels, ads, analytics, re-renders)
        # would otherwise constantly unblock LLMDetection without the
        # operator ever touching the page. No mousemove: hover is presence,
        # not action.
        from browser_handoff.detection.llm import _activity_setup_js

        js = _activity_setup_js("__bh_abc123")
        assert "MutationObserver" not in js
        assert "addEventListener" in js
        assert "capture: true" in js
        assert "passive: true" in js
        for ev in (
            "mousedown", "keydown", "wheel", "touchstart", "scroll",
            "input", "paste",
        ):
            assert ev in js
        assert "mousemove" not in js


class TestSessionPresence:
    """The presence primitive that gates orchestration's callback.

    Owned by HandoffSession, bumped on each `presence` message from the
    wrapper (and on first accept). Handoff.wait_for_completion awaits
    `wait_until_connected()` before installing detection listeners and
    reads `state` before calling detection.check — so a detection
    scheduled to fire while the operator has wandered off doesn't burn
    the LLM call.
    """

    def test_starts_inactive(self):
        from browser_handoff.server import SessionPresence

        p = SessionPresence()
        assert p.last_ping_ts is None
        assert p.state == "inactive"
        # _connected is internal but worth pinning here — a fresh
        # presence must NOT be set, otherwise wait_until_connected
        # would skip the gate.
        assert p._connected.is_set() is False

    def test_bump_makes_present_and_flips_connected(self):
        from browser_handoff.server import SessionPresence

        p = SessionPresence(freshness_threshold=1.0)
        p.bump()
        assert p.last_ping_ts is not None
        assert p.state == "present"
        assert p._connected.is_set() is True

    def test_state_decays_to_stale_past_threshold(self):
        from browser_handoff.server import SessionPresence

        p = SessionPresence(freshness_threshold=0.05)
        p.bump()
        assert p.state == "present"
        time.sleep(0.1)
        assert p.state == "stale"
        # But the connected gate stays open — first-connect is a one-shot,
        # not a freshness signal. Re-bumping doesn't have to happen for
        # wait_until_connected to keep returning immediately.
        assert p._connected.is_set() is True

    async def test_wait_until_connected_blocks_then_returns(self):
        import asyncio
        from browser_handoff.server import SessionPresence

        p = SessionPresence()
        waiter = asyncio.create_task(p.wait_until_connected())
        await asyncio.sleep(0.02)
        assert not waiter.done(), "should block until first bump"
        p.bump()
        await asyncio.wait_for(waiter, timeout=0.5)

    async def test_wait_until_connected_returns_immediately_after_bump(self):
        from browser_handoff.server import SessionPresence

        p = SessionPresence()
        p.bump()
        await asyncio.wait_for(p.wait_until_connected(), timeout=0.1)


class TestHandoffSessionPresenceField:
    """HandoffSession defaults a SessionPresence so callers don't have to."""

    def test_session_starts_with_default_presence(self):
        session = _bare_session()
        assert session.presence.last_ping_ts is None
        assert session.presence.state == "inactive"
        assert session.presence._connected.is_set() is False
