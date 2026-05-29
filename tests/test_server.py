"""Tests for server configuration and the streaming server."""

import asyncio
import contextlib

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
        assert config.completion_timeout == 600.0
        assert config.jpeg_quality == 75
        assert config.every_nth_frame == 1

    def test_custom_values(self):
        """Test custom configuration values."""
        config = ServerConfig(
            host="localhost",
            port=3000,
            public_base="https://proxy.example.com",
            completion_timeout=300.0,
        )
        assert config.host == "localhost"
        assert config.port == 3000
        assert config.public_base == "https://proxy.example.com"
        assert config.completion_timeout == 300.0

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
            completion_timeout=120.0,
        )
        data = config.to_dict()
        assert data == {
            "host": "0.0.0.0",
            "port": 8080,
            "public_base": "https://example.com",
            "completion_timeout": 120.0,
            "jpeg_quality": 75,
            "every_nth_frame": 1,
        }

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "host": "127.0.0.1",
            "port": 9000,
            "public_base": "https://proxy.test.com",
            "completion_timeout": 60.0,
        }
        config = ServerConfig.from_dict(data)
        assert config.host == "127.0.0.1"
        assert config.port == 9000
        assert config.public_base == "https://proxy.test.com"
        assert config.completion_timeout == 60.0

    def test_from_dict_defaults(self):
        """Test from_dict with missing values uses defaults."""
        config = ServerConfig.from_dict({})
        assert config.host == "127.0.0.1"
        assert config.port == 8080
        assert config.public_base is None
        assert config.completion_timeout == 600.0

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
