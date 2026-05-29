"""Integration tests for LLMDetection.

Makes real LLM API calls. Skipped unless both LLM_ID and LLM_API_KEY
env vars are set.

Examples:
    LLM_ID=anthropic/claude-sonnet-4-5 LLM_API_KEY=sk-ant-... uv run pytest
    LLM_ID=openai/gpt-4o             LLM_API_KEY=sk-...      uv run pytest
    LLM_ID=gemini/gemini-2.0-flash   LLM_API_KEY=AIza...     uv run pytest
"""

from __future__ import annotations

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


# NOTE: the activity-debounced watch loop (register_listeners) is covered,
# browser-side and without any model call, in test_llm_activity.py — it needs
# neither LLM_ID nor LLM_API_KEY, so it isn't gated like the check() tests here.
