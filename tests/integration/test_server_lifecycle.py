"""Integration tests for the shared streaming-server lifecycle.

Unlike the detection tests, these spin up the *real* StreamingServer (uvicorn)
and drive complete handoffs through a single Handoff instance. The goal is to
prove the ref-counted shared-server model: concurrent handoffs run as separate
sessions on one server/port (distinguished by session id) and never collide,
and the server starts on the first handoff and stops when the last finishes.

These intentionally reach into a few private attributes (`_server`,
`_session_count`, `server.sessions`) — that internal state *is* the thing under
test here.
"""

from __future__ import annotations

import asyncio
import socket

from playwright.async_api import Browser

from browser_handoff import Handoff, ServerConfig
from browser_handoff.detection import Detection


def _free_port() -> int:
    """Grab an ephemeral port for the streaming server.

    Small TOCTOU window between close and re-bind, but fine for a test and far
    safer than hardcoding a port that might be in use.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _wait_until(predicate, timeout: float = 15.0, interval: float = 0.02) -> None:
    """Poll predicate() until it returns true, or raise on timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def test_concurrent_handoffs_share_one_server(
    browser: Browser, base_url: str
) -> None:
    """Two overlapping handoffs on one Handoff stream on the same port with
    distinct session ids, then the server tears down once both finish."""
    port = _free_port()
    handoff = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

    # Independent contexts/pages, each starting away from the completion URL so
    # the handoff actually waits (rather than completing on the entry check).
    ctx1 = await browser.new_context()
    ctx2 = await browser.new_context()
    page1 = await ctx1.new_page()
    page2 = await ctx2.new_page()
    h1 = h2 = None
    try:
        await page1.goto(f"{base_url}/login")
        await page2.goto(f"{base_url}/login")

        complete = Detection.url(path_contains=["/dashboard"])

        # Fire both handoffs concurrently on the SAME Handoff instance.
        h1 = asyncio.create_task(
            handoff.wait_for_completion(page1, on=complete, name="one")
        )
        h2 = asyncio.create_task(
            handoff.wait_for_completion(page2, on=complete, name="two")
        )

        # Wait until *both* sessions are actually registered on the shared
        # server. (Gate on the sessions dict, not _session_count — the count
        # is bumped on acquire, before register_session populates the dict.)
        await _wait_until(
            lambda: handoff._server is not None
            and len(handoff._server.sessions) == 2
        )

        assert handoff.is_serving, "shared server should be running"
        assert handoff.live_session_count == 2, "two live handoffs expected"

        # Inspect sessions directly: the "distinct sessions on one shared
        # server" invariant is what's under test and has no public accessor.
        sessions = list(handoff._server.sessions.values())
        assert len(sessions) == 2, f"expected 2 sessions, got {len(sessions)}"
        tokens = [s.access_token for s in sessions]
        assert len(set(tokens)) == 2, "tokens must be unique per session"

        # Each session is reachable over the wire by its capability token on
        # that single port; an unknown token is not. (page.request is an
        # independent HTTP client — it doesn't navigate the streamed page.)
        for token in tokens:
            resp = await page1.request.get(f"http://127.0.0.1:{port}/?t={token}")
            assert resp.status == 200, "valid token should serve on the shared port"
        bogus = await page1.request.get(f"http://127.0.0.1:{port}/?t=does-not-exist")
        assert bogus.status == 404, "unknown token must not resolve"

        # Give both handoffs a beat to arm their completion listeners, then
        # complete each by navigating to the completion URL.
        await asyncio.sleep(0.3)
        await page1.goto(f"{base_url}/dashboard")
        await page2.goto(f"{base_url}/dashboard")

        r1, r2 = await asyncio.wait_for(asyncio.gather(h1, h2), timeout=10)
        assert r1.was_blocked and not r1.timed_out
        assert r2.was_blocked and not r2.timed_out

        # Last session out → server torn down, ready to lazily restart.
        await _wait_until(
            lambda: not handoff.is_serving and handoff.live_session_count == 0
        )
    finally:
        # On a mid-test failure, cancel any still-running handoff and let its
        # teardown (unregister/release) settle against the still-open context
        # before we close it.
        pending = [t for t in (h1, h2) if t is not None and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await ctx1.close()
        await ctx2.close()


async def test_sequential_handoffs_restart_server_on_same_port(
    browser: Browser, base_url: str
) -> None:
    """Back-to-back handoffs on one Handoff: the server stops when the first
    finishes (count→0) and lazily restarts on the same fixed port for the
    second.

    Complements the concurrent test, which only proves the start side and the
    final teardown — this pins the stop→restart cycle that the ref-count model
    relies on, and that the freed port rebinds cleanly in practice. The Handoff
    holds one (frozen) ServerConfig, so a successful restart necessarily reuses
    the same port."""
    port = _free_port()
    handoff = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

    ctx = await browser.new_context()
    page = await ctx.new_page()
    h = None
    try:
        complete = Detection.url(path_contains=["/dashboard"])

        async def run_one_handoff() -> None:
            """Drive a single handoff to completion, asserting the server comes
            up for it and tears down afterward."""
            nonlocal h
            await page.goto(f"{base_url}/login")
            h = asyncio.create_task(handoff.wait_for_completion(page, on=complete))

            # Server starts lazily for this handoff.
            await _wait_until(
                lambda: handoff.is_serving and handoff.live_session_count == 1
            )

            # Complete it and confirm the server is fully gone afterward.
            await asyncio.sleep(0.3)  # let the completion listener arm
            await page.goto(f"{base_url}/dashboard")
            result = await asyncio.wait_for(h, timeout=10)
            assert result.was_blocked and not result.timed_out
            await _wait_until(
                lambda: not handoff.is_serving and handoff.live_session_count == 0
            )

        # First handoff: server starts, runs, stops.
        await run_one_handoff()
        # Second handoff on the SAME instance: server must lazily restart and
        # rebind the now-freed port. If teardown left the socket bound (or the
        # task lingering), this re-acquire would hang or fail.
        await run_one_handoff()
    finally:
        if h is not None and not h.done():
            h.cancel()
            await asyncio.gather(h, return_exceptions=True)
        await ctx.close()
