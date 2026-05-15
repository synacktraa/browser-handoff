"""Integration tests for LLMDetection.

Makes real LLM API calls. Skipped unless both LLM_ID and LLM_API_KEY
env vars are set.

Examples:
    LLM_ID=anthropic/claude-sonnet-4-5 LLM_API_KEY=sk-ant-... uv run pytest
    LLM_ID=openai/gpt-4o             LLM_API_KEY=sk-...      uv run pytest
    LLM_ID=gemini/gemini-2.0-flash   LLM_API_KEY=AIza...     uv run pytest
"""

from __future__ import annotations

import asyncio
import os

import pytest
from playwright.async_api import Page

from browser_handoff.detection import Detection

_LLM_ID = os.environ.get("LLM_ID")
_LLM_API_KEY = os.environ.get("LLM_API_KEY")

pytestmark = pytest.mark.skipif(
    not (_LLM_ID and _LLM_API_KEY),
    reason="LLM_ID and LLM_API_KEY env vars not set",
)


# ---- check() match semantics --------------------------------------------


async def test_check_matches_login_page(page: Page, base_url: str) -> None:
    """LLM correctly identifies a login form on the page."""
    await page.goto(f"{base_url}/login")
    detection = Detection.llm(
        model=_LLM_ID,
        condition="The page is showing a login form with email and password fields",
        api_key=_LLM_API_KEY,
    )
    result = await detection.check(page)
    assert result.matched, f"expected match, got: {result.reason}"


async def test_check_does_not_match_dashboard(page: Page, base_url: str) -> None:
    """LLM correctly says no on a non-login page."""
    await page.goto(f"{base_url}/dashboard")
    detection = Detection.llm(
        model=_LLM_ID,
        condition="The page is showing a login form with email and password fields",
        api_key=_LLM_API_KEY,
    )
    result = await detection.check(page)
    assert not result.matched, f"expected no match, got: {result.reason}"


# ---- listener registration / cleanup ------------------------------------


async def test_cleanup_stops_polling(page: Page, base_url: str) -> None:
    """register_listeners' periodic screenshot task must stop on cleanup.

    Without proper cleanup, the polling coroutine would keep taking
    screenshots (and potentially calling the LLM) forever.
    """
    await page.goto(f"{base_url}/dynamic")
    detection = Detection.llm(
        model=_LLM_ID,
        condition="anything",  # we don't call check() — only test the loop
        api_key=_LLM_API_KEY,
    )
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    # First iteration runs immediately because _last_frame_hash is None.
    await asyncio.sleep(0.5)
    assert len(calls) >= 1, "initial screenshot didn't fire callback"

    cleanup()
    snapshot = len(calls)

    # Default poll interval is 2s — wait longer and confirm no more fires.
    await asyncio.sleep(2.5)
    assert len(calls) == snapshot, "polling task still firing after cleanup"
