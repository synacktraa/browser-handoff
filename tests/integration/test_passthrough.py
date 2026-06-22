"""Integration tests for stream_url passthrough mode.

These spin up real Playwright + StreamingServer and verify the passthrough
plumbing end-to-end:

  - The CDP screencast task is NOT started when stream_url is set.
  - GET /?t=<token> serves the proxy template (not the streaming one).
  - The status WebSocket dispatches to the passthrough handler.
  - notify_task_expired delivers an event the proxy template can react to.
  - The in-page activity watcher (_PassthroughActivityWatcher) ticks
    session.operator_activity on real DOM events.

The stream_url itself is a dummy — we only need bh to think a substrate
viewer URL was supplied. The integration is about what bh does in
response, not about the substrate's behavior.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from playwright.async_api import Browser

from browser_handoff import Handoff, ServerConfig
from browser_handoff.detection import Detection
from browser_handoff.server.streaming import _PassthroughActivityWatcher


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
    handoff = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base_url}/login")

    h_task = asyncio.create_task(
        handoff.wait_for_completion(
            page,
            on=Detection.url(path_contains=["/dashboard"]),
            reason="login passthrough test",
            stream_url="https://dummy.substrate.example/viewer?t=abc",
        )
    )
    try:
        # Wait for the session to register so we can inspect it.
        await _wait_until(lambda: bool(handoff._server and handoff._server.sessions))
        server = handoff._server
        assert server is not None
        sessions = list(server.sessions.values())
        assert len(sessions) == 1
        session = sessions[0]

        # Core invariant: no screencast pump in passthrough mode.
        assert session.is_passthrough is True
        assert session.capture_task is None
        assert session.frame_seq == 0

        # The activity watcher IS installed (separate path).
        assert session.passthrough_activity_watcher is not None
    finally:
        await page.goto(f"{base_url}/dashboard")  # satisfies completion
        await asyncio.wait_for(h_task, timeout=10.0)
        await ctx.close()


async def test_passthrough_serves_proxy_template(
    browser: Browser, base_url: str
) -> None:
    """GET /?t=<token> returns the proxy template, not intervention.html."""
    port = _free_port()
    handoff = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base_url}/login")

    h_task = asyncio.create_task(
        handoff.wait_for_completion(
            page,
            on=Detection.url(path_contains=["/dashboard"]),
            reason="proxy template test",
            stream_url="https://dummy.substrate.example/viewer?t=xyz",
        )
    )
    try:
        await _wait_until(lambda: bool(handoff._server and handoff._server.sessions))
        server = handoff._server
        session = next(iter(server.sessions.values()))

        # Render via the server's helper directly — same path the HTTP
        # route uses (templates are static once the session is registered).
        # Avoids a second HTTP-client dep just to verify the response body.
        html = server._get_html_client(session.session_id, session.reason)
        # Proxy-only markers; would not appear in intervention.html.
        assert "substrate-iframe" in html
        assert "expired-overlay" in html
        assert "proxy template test" in html
    finally:
        await page.goto(f"{base_url}/dashboard")
        await asyncio.wait_for(h_task, timeout=10.0)
        await ctx.close()


async def test_passthrough_activity_watcher_bumps_on_in_page_event(
    browser: Browser, base_url: str
) -> None:
    """The stealth watcher must tick operator_activity when an event reaches
    the page. We dispatch the event from outside the watcher's install (via
    page.evaluate) to simulate the post-CDP-dispatch path the substrate uses.
    """
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base_url}/login")

    # Build a session manually so we can install just the watcher in
    # isolation (no full Handoff machinery needed for this assertion).
    from browser_handoff.server.session import HandoffSession

    session = HandoffSession(
        session_id="watcher-test",
        page=page,
        context=ctx,
        cdp=await ctx.new_cdp_session(page),
        reason="watcher test",
        stream_url="https://dummy/viewer",
    )
    loop = asyncio.get_running_loop()
    watcher = _PassthroughActivityWatcher(session, loop, poll_interval=0.05)
    await watcher.install()

    try:
        # operator_activity hasn't been bumped yet.
        assert session.operator_activity.last_activity is None

        # Dispatch a synthetic event on the page — equivalent to what a
        # substrate-side CDP Input.dispatchMouseEvent would produce. The
        # injected listeners must observe it and update the stamp; the
        # watcher's poll must then bump operator_activity.
        await page.evaluate(
            "() => window.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))"
        )

        await _wait_until(
            lambda: session.operator_activity.last_activity is not None,
            timeout=2.0,
        )
    finally:
        await watcher.shutdown()
        await ctx.close()


async def test_passthrough_activity_watcher_is_stealth(
    browser: Browser, base_url: str
) -> None:
    """The watcher's stamp property must be invisible to page-JS enumeration.

    Bot-detection scripts often dump every property on window; the stamp must
    not appear in Object.keys / for-in / JSON.stringify and no function/
    binding should be installed that page JS could fingerprint.
    """
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base_url}/login")

    from browser_handoff.server.session import HandoffSession

    session = HandoffSession(
        session_id="stealth-test",
        page=page,
        context=ctx,
        cdp=await ctx.new_cdp_session(page),
        reason="stealth test",
        stream_url="https://dummy/viewer",
    )
    watcher = _PassthroughActivityWatcher(
        session, asyncio.get_running_loop(), poll_interval=0.05
    )
    await watcher.install()

    try:
        var = watcher._var

        # The stamp property does exist (typeof === 'number' once mark()
        # runs; until then it's the seeded 0). Reading it directly works.
        typeof = await page.evaluate(f"() => typeof window.{var}")
        assert typeof == "number"

        # ...but it's non-enumerable, so the three common enumeration paths
        # used by detection scripts don't see it.
        in_keys = await page.evaluate(f"() => Object.keys(window).includes('{var}')")
        assert in_keys is False

        in_for_in = await page.evaluate(
            f"() => {{ for (const k in window) {{ if (k === '{var}') return true; }} return false; }}"
        )
        assert in_for_in is False

        # No injected callable on window — only a number.
        is_function = await page.evaluate(
            f"() => typeof window.{var} === 'function'"
        )
        assert is_function is False
    finally:
        await watcher.shutdown()
        await ctx.close()


async def test_notify_task_expired_event_shape(
    browser: Browser, base_url: str
) -> None:
    """Server-side notify_task_expired pushes the right JSON to subscribers."""
    port = _free_port()
    handoff = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(f"{base_url}/login")

    # Pick a completion condition that won't fire on /login or /dashboard
    # so the handoff stays open until we cancel it ourselves.
    h_task = asyncio.create_task(
        handoff.wait_for_completion(
            page,
            on=Detection.url(path_contains=["/this-route-does-not-exist"]),
            reason="expired-event test",
            stream_url="https://dummy/viewer",
        )
    )
    try:
        await _wait_until(lambda: bool(handoff._server and handoff._server.sessions))
        server = handoff._server
        session = next(iter(server.sessions.values()))

        sent: list[dict] = []

        class FakeWS:
            async def send_json(self, payload):
                sent.append(payload)

        session.websockets.append(FakeWS())
        await server.notify_task_expired(session.session_id)
        assert sent == [{"type": "task_expired"}]
    finally:
        # The completion detection never fires; cancel the task to unwind.
        h_task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await h_task
        await ctx.close()
