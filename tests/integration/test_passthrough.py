"""Integration tests for stream_url passthrough mode.

These spin up real Playwright + StreamingServer and verify the passthrough
plumbing end-to-end:

  - The CDP screencast task is NOT started when stream_url is set.
  - GET /?t=<token> serves the proxy template (not the streaming one).
  - The status WebSocket dispatches to the passthrough handler.
  - notify_task_expired delivers an event the proxy template can react to.

In-page activity observation moved into LLMDetection's unified watcher
after the v0.6 refactor — the stealth observer and bump behavior are
covered by tests/integration/test_llm_activity.py against real input
events. The stream_url itself is a dummy — we only need bh to think a
substrate viewer URL was supplied. The integration is about what bh does
in response, not about the substrate's behavior.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from playwright.async_api import Browser

from browser_handoff import Handoff, ServerConfig
from browser_handoff.detection import Detection


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def test_passthrough_skips_screencast_pump(
    browser: Browser, base_url: str
) -> None:
    """When stream_url is set, register_session must not schedule capture_task.

    The session's frame_seq stays at 0 (no frames produced) and capture_task
    remains None. Streaming-mode comparison test in test_screencast_input.py
    confirms the inverse — pump runs without stream_url.
    """
    port = _free_port()
    h = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base_url}/login")

    h_task = asyncio.create_task(
        h.pause(
            page,
            on=Detection.url(path_contains=["/dashboard"]),
            reason="login passthrough test",
            stream_url="https://dummy.substrate.example/viewer?t=abc",
        )
    )
    try:
        # Wait for the session to register so we can inspect it.
        await _wait_until(lambda: bool(h._server and h._server.sessions))
        server = h._server
        assert server is not None
        sessions = list(server.sessions.values())
        assert len(sessions) == 1
        session = sessions[0]

        # Core invariant: no screencast pump in passthrough mode.
        assert session.is_passthrough is True
        assert session.capture_task is None
        assert session.frame_seq == 0
    finally:
        # Simulate an operator opening the wrapper so pause
        # advances past its lazy-install gate AND so the freshness check
        # in the orchestration callback passes. One bump covers both:
        # SessionPresence flips the connect event on first call. The WS
        # handler does this on accept; bypassing it directly is what
        # these plumbing-level tests need (no real wrapper).
        session.presence.bump()
        await asyncio.sleep(0.1)  # listener registers on next loop turn
        await page.goto(f"{base_url}/dashboard")  # satisfies completion
        await asyncio.wait_for(h_task, timeout=10.0)
        await ctx.close()


async def test_passthrough_serves_proxy_template(
    browser: Browser, base_url: str
) -> None:
    """GET /?t=<token> returns the proxy template, not intervention.html."""
    port = _free_port()
    h = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base_url}/login")

    h_task = asyncio.create_task(
        h.pause(
            page,
            on=Detection.url(path_contains=["/dashboard"]),
            reason="proxy template test",
            stream_url="https://dummy.substrate.example/viewer?t=xyz",
        )
    )
    try:
        await _wait_until(lambda: bool(h._server and h._server.sessions))
        server = h._server
        session = next(iter(server.sessions.values()))

        # Render via the server's helper directly — same path the HTTP
        # route uses (templates are static once the session is registered).
        # Avoids a second HTTP-client dep just to verify the response body.
        html = server._get_html_client(session.session_id, session.reason)
        # Proxy-only markers; would not appear in intervention.html.
        assert "substrate-iframe" in html
        assert "fallback-screenshot" in html
        assert "proxy template test" in html
    finally:
        # Simulate operator opening the wrapper. One bump flips the
        # connect gate AND records the freshness timestamp.
        session.presence.bump()
        await asyncio.sleep(0.1)
        await page.goto(f"{base_url}/dashboard")
        await asyncio.wait_for(h_task, timeout=10.0)
        await ctx.close()


async def test_notify_task_expired_event_shape(
    browser: Browser, base_url: str
) -> None:
    """Server-side notify_task_expired pushes the right JSON to subscribers."""
    port = _free_port()
    h = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base_url}/login")

    # Pick a completion condition that won't fire on /login or /dashboard
    # so the handoff stays open until we cancel it ourselves.
    h_task = asyncio.create_task(
        h.pause(
            page,
            on=Detection.url(path_contains=["/this-route-does-not-exist"]),
            reason="expired-event test",
            stream_url="https://dummy/viewer",
        )
    )
    try:
        await _wait_until(lambda: bool(h._server and h._server.sessions))
        server = h._server
        session = next(iter(server.sessions.values()))

        sent: list[dict] = []

        class FakeWS:
            async def send_json(self, payload):
                sent.append(payload)

        session.websockets.append(FakeWS())
        await server.notify_task_expired(session.session_id)
        assert len(sent) == 1
        assert sent[0]["type"] == "task_expired"
        # The server captures a screenshot for passthrough sessions and
        # embeds it as a base64 data URL so the wrapper can swap the
        # iframe out for the last-known page state. Page exists in this
        # test (real Playwright session), so the field is present.
        assert sent[0].get("screenshot", "").startswith("data:image/jpeg;base64,")
    finally:
        # The completion detection never fires; cancel the task to unwind.
        h_task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await h_task
        await ctx.close()
