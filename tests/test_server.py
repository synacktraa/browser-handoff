"""Tests for server configuration and the streaming server."""

import asyncio
import contextlib
import time

import pytest
from starlette.middleware.cors import CORSMiddleware

from browser_handoff.server import ServerConfig
from browser_handoff.server.session import HandoffSession
from browser_handoff.server.streaming import StreamingServer


def _bare_session() -> HandoffSession:
    """A HandoffSession with placeholder page/context/cdp.

    _spawn_tracked only touches session.background_tasks, so the browser
    objects can be inert stand-ins."""
    return HandoffSession(
        session_id="test",
        page=object(),
        context=object(),
        cdp=object(),
        reason="test",
    )


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

    def test_stream_url_carries_token_not_session_id(self):
        server = StreamingServer()
        session = self._register(server, expires_at=time.time() + 60)
        url = server.get_stream_url(session.session_id)
        assert f"?t={session.access_token}" in url
        assert "?session=" not in url  # the id is no longer the URL gate


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
