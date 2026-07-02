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
    h = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

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
            h.pause(page1, until=complete, name="one")
        )
        h2 = asyncio.create_task(
            h.pause(page2, until=complete, name="two")
        )

        # Wait until *both* sessions are actually registered on the shared
        # server. (Gate on the sessions dict, not _session_count — the count
        # is bumped on acquire, before register_session populates the dict.)
        await _wait_until(
            lambda: h._server is not None
            and len(h._server.sessions) == 2
        )

        assert h.is_serving, "shared server should be running"
        assert h.live_session_count == 2, "two live handoffs expected"

        # Inspect sessions directly: the "distinct sessions on one shared
        # server" invariant is what's under test and has no public accessor.
        sessions = list(h._server.sessions.values())
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

        # Simulate operators opening each wrapper. One bump on each
        # session's presence flips the connect gate (so the URL listener
        # registers) AND satisfies the freshness check in the callback.
        # The WS handler does this on first accept; bypassing it directly
        # is fine here since the assertion is about the shared server,
        # not the WS protocol. Give the listeners a beat to arm before
        # completing via navigation.
        for s in sessions:
            s.presence.bump()
        await asyncio.sleep(0.3)
        await page1.goto(f"{base_url}/dashboard")
        await page2.goto(f"{base_url}/dashboard")

        r1, r2 = await asyncio.wait_for(asyncio.gather(h1, h2), timeout=10)
        assert r1.was_blocked and not r1.timed_out
        assert r2.was_blocked and not r2.timed_out

        # Last session out → server torn down, ready to lazily restart.
        await _wait_until(
            lambda: not h.is_serving and h.live_session_count == 0
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


async def test_access_timeout_fires_without_connect(
    browser: Browser, base_url: str
) -> None:
    """Operator never connects → access timer fires, result carries
    timeout_cause='access'."""
    port = _free_port()
    h = Handoff(
        server=ServerConfig(
            host="127.0.0.1",
            port=port,
            access_timeout=0.5,
            completion_timeout=10.0,
        ),
    )

    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        await page.goto(f"{base_url}/login")
        # Never bump presence; access timer should fire.
        result = await asyncio.wait_for(
            h.pause(
                page, until=Detection.url(path_contains=["/dashboard"]),
            ),
            timeout=10,
        )
        assert result.was_blocked is True
        assert result.timed_out is True
        assert result.timeout_cause == "access"
    finally:
        await ctx.close()


async def test_completion_timeout_fires_after_connect(
    browser: Browser, base_url: str
) -> None:
    """Operator connects but never satisfies detection → completion
    timer fires, result carries timeout_cause='completion'."""
    port = _free_port()
    h = Handoff(
        server=ServerConfig(
            host="127.0.0.1",
            port=port,
            access_timeout=10.0,
            completion_timeout=0.5,
        ),
    )

    ctx = await browser.new_context()
    page = await ctx.new_page()
    h = None
    try:
        await page.goto(f"{base_url}/login")
        complete = Detection.url(path_contains=["/dashboard"])
        h = asyncio.create_task(h.pause(page, until=complete))

        # Simulate the operator opening the wrapper. Once registered,
        # bump presence so the access timer retires and the completion
        # timer starts. The completion timer (0.5s) then fires.
        await _wait_until(
            lambda: h.is_serving and bool(h._server.sessions)
        )
        session = next(iter(h._server.sessions.values()))
        session.presence.bump()

        result = await asyncio.wait_for(h, timeout=10)
        assert result.was_blocked is True
        assert result.timed_out is True
        assert result.timeout_cause == "completion"
    finally:
        if h is not None and not h.done():
            h.cancel()
            await asyncio.gather(h, return_exceptions=True)
        await ctx.close()


async def test_completion_timer_anchors_on_first_connect(
    browser: Browser, base_url: str
) -> None:
    """A reconnect (second bump) must not reset the completion timer."""
    port = _free_port()
    h = Handoff(
        server=ServerConfig(
            host="127.0.0.1",
            port=port,
            access_timeout=10.0,
            completion_timeout=1.5,
        ),
    )

    ctx = await browser.new_context()
    page = await ctx.new_page()
    h = None
    try:
        await page.goto(f"{base_url}/login")
        complete = Detection.url(path_contains=["/dashboard"])
        h = asyncio.create_task(h.pause(page, until=complete))

        await _wait_until(
            lambda: h.is_serving and bool(h._server.sessions)
        )
        session = next(iter(h._server.sessions.values()))
        start = asyncio.get_running_loop().time()
        session.presence.bump()
        await asyncio.sleep(0.8)
        session.presence.bump()  # reconnect — must not reset

        result = await asyncio.wait_for(h, timeout=10)
        elapsed = asyncio.get_running_loop().time() - start
        assert result.timeout_cause == "completion"
        # Anchored to first bump: fires ~1.5s elapsed.
        # Reset bug: fires ~0.8+1.5 = 2.3s elapsed.
        # Threshold 2.0 sits midway with ~0.5s slack on each side —
        # enough headroom for scheduler jitter on slow runners.
        assert elapsed < 2.0, f"completion timer may have reset (elapsed={elapsed:.3f})"
    finally:
        if h is not None and not h.done():
            h.cancel()
            await asyncio.gather(h, return_exceptions=True)
        await ctx.close()


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
    h = Handoff(server=ServerConfig(host="127.0.0.1", port=port))

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
            h = asyncio.create_task(h.pause(page, until=complete))

            # Server starts lazily for this h. Gate on the sessions
            # dict (not live_session_count) — _acquire_server bumps the
            # count before register_session populates the dict, so the
            # count flipping is not yet safe to dereference.
            await _wait_until(
                lambda: h.is_serving
                and bool(h._server.sessions)
            )
            assert h.live_session_count == 1

            # Simulate the operator opening the wrapper. One bump flips
            # the connect gate (so pause registers the URL
            # listener) AND records the freshness timestamp. The WS
            # handler does this on first accept; bypassing it directly is
            # fine here since the assertion is about server lifecycle.
            session = next(iter(h._server.sessions.values()))
            session.presence.bump()

            # Complete it and confirm the server is fully gone afterward.
            await asyncio.sleep(0.3)  # let the completion listener arm
            await page.goto(f"{base_url}/dashboard")
            result = await asyncio.wait_for(h, timeout=10)
            assert result.was_blocked and not result.timed_out
            await _wait_until(
                lambda: not h.is_serving and h.live_session_count == 0
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
