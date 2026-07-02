"""Integration tests for ContentDetection against a real Chromium."""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from browser_handoff import Detection


# ---- check() match semantics --------------------------------------------


async def test_title_contains_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    assert (await Detection.content(title_contains=["Sign In"]).check(page)).matched


async def test_title_contains_no_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dashboard")
    assert not (await Detection.content(title_contains=["Sign In"]).check(page)).matched


async def test_title_contains_any_substring_matches(page: Page, base_url: str) -> None:
    """title_contains uses OR semantics — any substring matching is enough."""
    await page.goto(f"{base_url}/dashboard")
    detection = Detection.content(title_contains=["Login", "Dashboard"])
    assert (await detection.check(page)).matched


async def test_title_matches_regex(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/payment")
    detection = Detection.content(title_matches=[r"Confirm.*Payment"])
    assert (await detection.check(page)).matched


async def test_title_matches_regex_no_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.content(title_matches=[r"^Dashboard$"])
    assert not (await detection.check(page)).matched


async def test_body_contains_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dashboard")
    assert (await Detection.content(body_contains=["Welcome back"]).check(page)).matched


async def test_body_contains_no_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dashboard")
    assert not (await Detection.content(body_contains=["please log in"]).check(page)).matched


async def test_body_matches_regex(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.content(body_matches=[r"Please.*log in"])
    assert (await detection.check(page)).matched


async def test_any_field_match_is_enough(page: Page, base_url: str) -> None:
    """ContentDetection uses OR across all four field types."""
    await page.goto(f"{base_url}/login")
    # Title doesn't contain "Confirm Payment", but body matches "log in".
    detection = Detection.content(
        title_contains=["Confirm Payment"],
        body_contains=["log in"],
    )
    assert (await detection.check(page)).matched


# ---- listener registration ----------------------------------------------


async def test_listener_fires_on_dom_loaded(page: Page, base_url: str) -> None:
    detection = Detection.content(title_contains=["Sign In"])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    try:
        await page.goto(f"{base_url}/login")
        await asyncio.sleep(0.15)
        assert len(calls) >= 1
    finally:
        cleanup()


async def test_listener_cleanup_actually_removes(page: Page, base_url: str) -> None:
    """Same lambda-vs-function bug class as URL detection."""
    detection = Detection.content(title_contains=["Sign In"])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    await page.goto(f"{base_url}/login")
    await asyncio.sleep(0.15)
    initial = len(calls)
    assert initial >= 1

    cleanup()

    await page.goto(f"{base_url}/dashboard")
    await asyncio.sleep(0.15)
    assert len(calls) == initial
