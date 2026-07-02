"""Integration tests for UrlDetection against a real Chromium."""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from browser_handoff.detection import Detection


# ---- check() match semantics --------------------------------------------


async def test_path_contains_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    assert (await Detection.url(path_contains=["/login"]).check(page)).matched


async def test_path_contains_no_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dashboard")
    assert not (await Detection.url(path_contains=["/login"]).check(page)).matched


async def test_path_matches_regex(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/payment")
    assert (await Detection.url(path_matches=[r"^/pay.*"]).check(page)).matched


async def test_path_matches_regex_no_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/payment")
    assert not (await Detection.url(path_matches=[r"^/login.*"]).check(page)).matched


async def test_host_equals(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/")
    assert (await Detection.url(host_equals=["127.0.0.1"]).check(page)).matched


async def test_host_equals_wrong_host(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/")
    assert not (await Detection.url(host_equals=["example.com"]).check(page)).matched


async def test_host_not_equals_excludes(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/")
    assert not (await Detection.url(host_not_equals=["127.0.0.1"]).check(page)).matched


async def test_host_not_equals_passes(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/")
    assert (await Detection.url(host_not_equals=["example.com"]).check(page)).matched


async def test_scheme_equals(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/")
    assert (await Detection.url(scheme_equals="http").check(page)).matched


async def test_scheme_equals_wrong_scheme(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/")
    assert not (await Detection.url(scheme_equals="https").check(page)).matched


async def test_query_contains_all_required(page: Page, base_url: str) -> None:
    """query_contains requires *all* listed substrings (AND semantics)."""
    await page.goto(f"{base_url}/login?code=abc&state=xyz")
    detection = Detection.url(query_contains=["code=", "state="])
    assert (await detection.check(page)).matched


async def test_query_contains_missing_one_fails(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login?code=abc")
    detection = Detection.url(query_contains=["code=", "state="])
    assert not (await detection.check(page)).matched


async def test_combined_constraints(page: Page, base_url: str) -> None:
    """Multiple constraints AND together within a single UrlDetection."""
    await page.goto(f"{base_url}/login?code=xyz")
    detection = Detection.url(
        host_equals=["127.0.0.1"],
        path_contains=["/login"],
        query_contains=["code="],
    )
    assert (await detection.check(page)).matched


# ---- listener registration ----------------------------------------------


async def test_listener_fires_on_navigation(page: Page, base_url: str) -> None:
    detection = Detection.url(path_contains=["/login"])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    try:
        await page.goto(f"{base_url}/login")
        await asyncio.sleep(0.1)  # framenavigated dispatch tick
        assert len(calls) >= 1
    finally:
        cleanup()


async def test_listener_cleanup_actually_removes(page: Page, base_url: str) -> None:
    """Critical: exercises the lambda-vs-function listener-cleanup bug.

    Pre-fix, the registered lambda and the function passed to
    remove_listener were different objects, so cleanup was a silent no-op
    and listeners stacked across handoff.guard() invocations.
    """
    detection = Detection.url(path_contains=["/login"])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    await page.goto(f"{base_url}/login")
    await asyncio.sleep(0.1)
    initial = len(calls)
    assert initial >= 1

    cleanup()

    await page.goto(f"{base_url}/dashboard")
    await asyncio.sleep(0.1)
    assert len(calls) == initial, (
        f"listener still firing after cleanup ({initial} -> {len(calls)})"
    )


async def test_listener_fires_once_per_navigation(page: Page, base_url: str) -> None:
    """No listener-stacking from a single register call."""
    detection = Detection.url(path_contains=["/login"])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    try:
        await page.goto(f"{base_url}/login")
        await asyncio.sleep(0.1)
        # Exactly one main-frame framenavigated event per goto.
        assert len(calls) == 1
    finally:
        cleanup()
