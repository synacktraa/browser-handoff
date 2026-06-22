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
        assert config.session_timeout == 600.0
        assert config.jpeg_quality == 75
        assert config.every_nth_frame == 1

    def test_custom_values(self):
        """Test custom configuration values."""
        config = ServerConfig(
            host="localhost",
            port=3000,
            public_base="https://proxy.example.com",
            session_timeout=300.0,
        )
        assert config.host == "localhost"
        assert config.port == 3000
        assert config.public_base == "https://proxy.example.com"
        assert config.session_timeout == 300.0

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
            session_timeout=120.0,
        )
        data = config.to_dict()
        assert data == {
            "host": "0.0.0.0",
            "port": 8080,
            "public_base": "https://example.com",
            "session_timeout": 120.0,
            "jpeg_quality": 75,
            "every_nth_frame": 1,
        }

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "host": "127.0.0.1",
            "port": 9000,
            "public_base": "https://proxy.test.com",
            "session_timeout": 60.0,
        }
        config = ServerConfig.from_dict(data)
        assert config.host == "127.0.0.1"
        assert config.port == 9000
        assert config.public_base == "https://proxy.test.com"
        assert config.session_timeout == 60.0

    def test_from_dict_defaults(self):
        """Test from_dict with missing values uses defaults."""
        config = ServerConfig.from_dict({})
        assert config.host == "127.0.0.1"
        assert config.port == 8080
        assert config.public_base is None
        assert config.session_timeout == 600.0

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


class TestSessionTimeoutDeprecation:
    """`completion_timeout` is renamed to `session_timeout` (deprecated alias).

    The value bounds the whole session/token lifetime now, not just the
    completion wait, so the name follows the meaning. The old name keeps
    working (with a warning when set) for one major cycle.
    """

    def test_session_timeout_is_canonical(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any deprecation here would fail
            assert ServerConfig().session_timeout == 600.0
            assert ServerConfig(session_timeout=300.0).session_timeout == 300.0

    def test_completion_timeout_still_readable_without_warning(self):
        # Reading the alias on a config built the new way must not warn and
        # must mirror session_timeout (old code keeps working).
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert ServerConfig(session_timeout=120.0).completion_timeout == 120.0

    def test_passing_completion_timeout_warns_and_applies(self):
        with pytest.warns(DeprecationWarning, match="session_timeout"):
            config = ServerConfig(completion_timeout=42.0)
        assert config.session_timeout == 42.0
        assert config.completion_timeout == 42.0  # alias mirrors it

    def test_to_dict_uses_new_name(self):
        data = ServerConfig(session_timeout=300.0).to_dict()
        assert data["session_timeout"] == 300.0
        assert "completion_timeout" not in data

    def test_from_dict_new_name(self):
        config = ServerConfig.from_dict({"session_timeout": 200.0})
        assert config.session_timeout == 200.0

    def test_from_dict_old_name_warns(self):
        with pytest.warns(DeprecationWarning, match="session_timeout"):
            config = ServerConfig.from_dict({"completion_timeout": 200.0})
        assert config.session_timeout == 200.0


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


class TestPassthroughActivityWatcher:
    """The stealth in-page activity watcher's lifecycle invariants.

    Functional behavior (DOM-mutation/input event observation) needs a real
    page and is covered by integration tests; these check the install/
    shutdown state machine and the JS shape.
    """

    async def _watcher(self):
        from browser_handoff.server.streaming import _PassthroughActivityWatcher

        # Watcher only touches the page during install/shutdown; the bare
        # session's page=object() is fine for the state-machine tests below.
        # Async helper so asyncio.get_running_loop() resolves the loop
        # pytest-asyncio set up for the test (no deprecation warning, no
        # falling back to get_event_loop()).
        session = _bare_session(stream_url="https://substrate/viewer")
        return _PassthroughActivityWatcher(session, asyncio.get_running_loop())

    async def test_setup_js_uses_non_enumerable_property(self):
        # The whole stealth claim hinges on this: site JS that walks
        # window must not see our stamp. Verify the injected JS uses
        # Object.defineProperty with enumerable:false.
        w = await self._watcher()
        js = w._setup_js()
        assert "Object.defineProperty(window," in js
        assert "enumerable: false" in js
        # And random, per-watcher name (no fixed bh_* string that detection
        # scripts could probe by name).
        assert w._var.startswith("__bh_")
        assert len(w._var) > len("__bh_")  # has a random suffix

    async def test_setup_js_attaches_mutation_observer_and_input_listeners(self):
        w = await self._watcher()
        js = w._setup_js()
        assert "MutationObserver" in js
        # Capture+passive input listeners — same stealth pattern as
        # detection/element.py and detection/llm.py.
        assert "addEventListener" in js
        assert "capture: true" in js
        assert "passive: true" in js
        # Events that signal real activity (excludes mousemove on purpose).
        for ev in ("mousedown", "keydown", "wheel", "touchstart", "scroll"):
            assert ev in js

    async def test_shutdown_before_install_is_noop(self):
        w = await self._watcher()
        # Never installed; shutdown must not raise or touch the page.
        await w.shutdown()
        assert w._installed is False
        assert w._poll_task is None
