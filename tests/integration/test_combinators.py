"""Integration tests for combinator detections (all/any/not)."""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from browser_handoff import Detection


# ---- match semantics ----------------------------------------------------


async def test_all_matches_when_all_children_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.all([
        Detection.url(path_contains=["/login"]),
        Detection.element(present=["input[type=password]"]),
    ])
    assert (await detection.check(page)).matched


async def test_all_fails_when_one_child_fails(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.all([
        Detection.url(path_contains=["/login"]),
        Detection.element(present=[".nonexistent"]),
    ])
    assert not (await detection.check(page)).matched


async def test_any_matches_when_one_child_matches(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.any([
        Detection.url(path_contains=["/nope"]),
        Detection.url(path_contains=["/login"]),
    ])
    assert (await detection.check(page)).matched


async def test_any_fails_when_no_child_matches(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.any([
        Detection.url(path_contains=["/nope"]),
        Detection.url(path_contains=["/also-nope"]),
    ])
    assert not (await detection.check(page)).matched


async def test_not_inverts_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.not_(Detection.url(path_contains=["/dashboard"]))
    assert (await detection.check(page)).matched


async def test_not_inverts_no_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.not_(Detection.url(path_contains=["/login"]))
    assert not (await detection.check(page)).matched


async def test_nested_all_any_not(page: Page, base_url: str) -> None:
    """all([any([url, url]), not_(element)]) — fully nested combinators."""
    await page.goto(f"{base_url}/login")
    detection = Detection.all([
        Detection.any([
            Detection.url(path_contains=["/login"]),
            Detection.url(path_contains=["/auth"]),
        ]),
        Detection.not_(Detection.element(present=[".user-menu"])),
    ])
    assert (await detection.check(page)).matched


async def test_nested_combinator_fails_when_inner_fails(
    page: Page, base_url: str
) -> None:
    await page.goto(f"{base_url}/login")
    detection = Detection.all([
        Detection.any([
            Detection.url(path_contains=["/login"]),
        ]),
        # /login DOES have a password input, so this not_(present) is false
        Detection.not_(Detection.element(present=["input[type=password]"])),
    ])
    assert not (await detection.check(page)).matched


# ---- listener wiring ----------------------------------------------------


async def test_combinator_listener_fires_on_url_child(
    page: Page, base_url: str
) -> None:
    """A combinator's register_listeners wires every child's events."""
    detection = Detection.any([
        Detection.url(path_contains=["/login"]),
        Detection.element(present=[".dynamic-item"]),
    ])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    try:
        await page.goto(f"{base_url}/login")
        await asyncio.sleep(0.15)
        assert len(calls) > 0, "URL child's listener didn't fire through combinator"
    finally:
        cleanup()


async def test_combinator_listener_fires_on_element_child(
    page: Page, base_url: str
) -> None:
    await page.goto(f"{base_url}/dynamic")

    detection = Detection.any([
        Detection.url(path_contains=["/never-match"]),
        Detection.element(present=[".dynamic-item"]),
    ])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    try:
        await asyncio.sleep(0.2)
        baseline = len(calls)

        await page.click("#add")
        await asyncio.sleep(0.3)

        assert len(calls) > baseline, (
            "element child's listener didn't fire through combinator"
        )
    finally:
        cleanup()


async def test_combinator_cleanup_removes_all_children(
    page: Page, base_url: str
) -> None:
    """Combinator's cleanup must propagate to every wrapped child."""
    detection = Detection.any([
        Detection.url(path_contains=["/login"]),
        Detection.url(path_contains=["/dashboard"]),
    ])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)

    await page.goto(f"{base_url}/login")
    await asyncio.sleep(0.15)
    snapshot = len(calls)

    cleanup()

    await page.goto(f"{base_url}/dashboard")
    await asyncio.sleep(0.15)
    assert len(calls) == snapshot, "child listeners survived combinator cleanup"


async def test_not_combinator_listener_propagates(page: Page, base_url: str) -> None:
    """Detection.not_(child) still wires the child's listener."""
    detection = Detection.not_(Detection.url(path_contains=["/never-match"]))
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    try:
        await page.goto(f"{base_url}/login")
        await asyncio.sleep(0.15)
        assert len(calls) > 0, "not_'s wrapped listener didn't fire"
    finally:
        cleanup()
