"""Integration tests for ElementDetection against a real Chromium.

This is the meatiest of the four — the previous implementation had
several silent bugs (multi-detection shadowing via expose_function,
broken cleanup, no re-injection after navigation). These tests pin
each of those down.

ElementDetection now uses a poll model (a MutationObserver stamps a hidden
window var; Python polls it) rather than expose_function, so each detection's
register_listeners is fully independent — see test_detection.py for the unit
coverage and test_no_detectable_instrumentation below for the stealth contract.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from browser_handoff.detection import Detection


# The Python side polls the activity stamp every ~100ms. Always wait at
# least this long after a DOM mutation before asserting a callback fired.
MUTATION_WAIT = 0.3


# ---- check() match semantics --------------------------------------------


async def test_present_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    assert (
        await Detection.element(present=["input[type=password]"]).check(page)
    ).matched


async def test_present_no_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dashboard")
    assert not (
        await Detection.element(present=["input[type=password]"]).check(page)
    ).matched


async def test_present_all_required(page: Page, base_url: str) -> None:
    """`present` is AND — all listed selectors must exist."""
    await page.goto(f"{base_url}/login")
    detection = Detection.element(present=["input[type=password]", "#nonexistent"])
    assert not (await detection.check(page)).matched


async def test_missing_match(page: Page, base_url: str) -> None:
    """A selector that's absent counts as 'missing'."""
    await page.goto(f"{base_url}/dashboard")
    assert (
        await Detection.element(missing=["input[type=password]"]).check(page)
    ).matched


async def test_missing_no_match_when_element_exists(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    assert not (
        await Detection.element(missing=["input[type=password]"]).check(page)
    ).matched


async def test_visible_match(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/login")
    assert (await Detection.element(visible=["#login-form"]).check(page)).matched


async def test_visible_no_match_when_display_none(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dynamic")
    await page.click("#add")
    await page.click("#hide")
    await asyncio.sleep(0.1)
    assert not (
        await Detection.element(visible=[".dynamic-item"]).check(page)
    ).matched


async def test_hidden_match_when_element_absent(page: Page, base_url: str) -> None:
    """`hidden` is satisfied if the element doesn't exist OR isn't visible."""
    await page.goto(f"{base_url}/login")
    assert (await Detection.element(hidden=[".nonexistent"]).check(page)).matched


async def test_hidden_match_when_display_none(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dynamic")
    await page.click("#add")
    await page.click("#hide")
    await asyncio.sleep(0.1)
    assert (await Detection.element(hidden=[".dynamic-item"]).check(page)).matched


async def test_hidden_no_match_when_visible(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dynamic")
    await page.click("#add")
    await asyncio.sleep(0.1)
    assert not (await Detection.element(hidden=[".dynamic-item"]).check(page)).matched


async def test_combined_constraints(page: Page, base_url: str) -> None:
    """All four constraint types AND together."""
    await page.goto(f"{base_url}/login")
    detection = Detection.element(
        present=["input[type=password]"],
        visible=["#login-form"],
        missing=[".user-menu"],
        hidden=[".nonexistent"],
    )
    assert (await detection.check(page)).matched


# ---- listener registration ----------------------------------------------


async def test_listener_fires_on_mutation(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/dynamic")
    await asyncio.sleep(0.1)  # let observer attach

    detection = Detection.element(present=[".dynamic-item"])
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = detection.register_listeners(page, cb)
    try:
        await asyncio.sleep(0.1)  # registration takes a tick
        baseline = len(calls)

        await page.click("#add")
        await asyncio.sleep(MUTATION_WAIT)

        assert len(calls) > baseline
    finally:
        cleanup()


async def test_multiple_detections_fire_independently(page: Page, base_url: str) -> None:
    """Two ElementDetections on the same page both fire on mutation.

    With the old expose_function model the second binding collided and only
    the first detection fired. The poll model makes each register_listeners
    independent (its own observer + var + poll loop), so both fire.
    """
    await page.goto(f"{base_url}/dynamic")

    calls_a: list = []
    calls_b: list = []

    async def cb_a(d):  # type: ignore[no-untyped-def]
        calls_a.append(d)

    async def cb_b(d):  # type: ignore[no-untyped-def]
        calls_b.append(d)

    cleanup_a = Detection.element(
        present=[".dynamic-item"]
    ).register_listeners(page, cb_a)
    cleanup_b = Detection.element(
        present=[".dynamic-item"]
    ).register_listeners(page, cb_b)
    try:
        await asyncio.sleep(0.2)
        baseline_a, baseline_b = len(calls_a), len(calls_b)

        await page.click("#add")
        await asyncio.sleep(MUTATION_WAIT)

        assert len(calls_a) > baseline_a, "first detection's callback didn't fire"
        assert len(calls_b) > baseline_b, (
            "second detection's callback didn't fire (silent shadowing)"
        )
    finally:
        cleanup_a()
        cleanup_b()


async def test_per_subscription_cleanup(page: Page, base_url: str) -> None:
    """Cleaning up one subscription doesn't affect the other."""
    await page.goto(f"{base_url}/dynamic")

    calls_a: list = []
    calls_b: list = []

    async def cb_a(d):  # type: ignore[no-untyped-def]
        calls_a.append(d)

    async def cb_b(d):  # type: ignore[no-untyped-def]
        calls_b.append(d)

    cleanup_a = Detection.element(
        present=[".dynamic-item"]
    ).register_listeners(page, cb_a)
    cleanup_b = Detection.element(
        present=[".dynamic-item"]
    ).register_listeners(page, cb_b)
    await asyncio.sleep(0.2)

    cleanup_a()

    baseline_a = len(calls_a)
    baseline_b = len(calls_b)

    await page.click("#add")
    await asyncio.sleep(MUTATION_WAIT)

    cleanup_b()

    assert len(calls_a) == baseline_a, "cleaned-up callback still fired"
    assert len(calls_b) > baseline_b, "remaining callback didn't fire"


async def test_re_registration_after_full_teardown(page: Page, base_url: str) -> None:
    """Fresh register on the same page after all subscribers cleanup.

    With no shared per-page binding to keep alive, a second registration on
    the same page after teardown just stands up its own observer + poll loop
    and works exactly like the first.
    """
    await page.goto(f"{base_url}/dynamic")

    # Round 1: register, fire, cleanup.
    calls1: list = []

    async def cb1(d):  # type: ignore[no-untyped-def]
        calls1.append(d)

    cleanup1 = Detection.element(
        present=[".dynamic-item"]
    ).register_listeners(page, cb1)
    await asyncio.sleep(0.2)
    baseline1 = len(calls1)
    await page.click("#add")
    await asyncio.sleep(MUTATION_WAIT)
    assert len(calls1) > baseline1
    cleanup1()

    # Round 2: re-register on the same page, fresh callback.
    calls2: list = []

    async def cb2(d):  # type: ignore[no-untyped-def]
        calls2.append(d)

    cleanup2 = Detection.element(
        present=[".dynamic-item"]
    ).register_listeners(page, cb2)
    try:
        await asyncio.sleep(0.2)
        baseline2 = len(calls2)
        await page.click("#add")
        await asyncio.sleep(MUTATION_WAIT)
        assert len(calls2) > baseline2, "re-registration after teardown didn't work"
    finally:
        cleanup2()


async def test_observer_reinjected_after_navigation(page: Page, base_url: str) -> None:
    """When the page navigates, the JS observer is reinjected automatically."""
    await page.goto(f"{base_url}/dynamic")

    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = Detection.element(
        present=[".dynamic-item"]
    ).register_listeners(page, cb)
    try:
        await asyncio.sleep(0.2)

        # Navigate again — JS context is reset, observer must be reinjected.
        await page.goto(f"{base_url}/dynamic")
        await asyncio.sleep(0.3)

        baseline = len(calls)
        await page.click("#add")
        await asyncio.sleep(MUTATION_WAIT)

        assert len(calls) > baseline, "observer not active after navigation"
    finally:
        cleanup()


# ---- stealth contract ----------------------------------------------------


async def test_no_detectable_instrumentation(page: Page, base_url: str) -> None:
    """The injected observer leaves nothing the page can find.

    Would fail on the old implementation: it set window.__handoffMutationObserver
    and __handoffMutationCallback (both enumerable) and bound a function via
    expose_function. The poll model uses a per-session, non-enumerable var and
    no binding, so none of those are observable from page JS.
    """
    await page.goto(f"{base_url}/dynamic")

    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = Detection.element(present=[".dynamic-item"]).register_listeners(page, cb)
    try:
        await asyncio.sleep(0.2)

        # Legacy fixed globals must be gone.
        assert (
            await page.evaluate("() => typeof window.__handoffMutationCallback")
        ) == "undefined"
        assert (
            await page.evaluate("() => typeof window.__handoffMutationObserver")
        ) == "undefined"

        # No instrumentation is enumerable on window.
        leaked = await page.evaluate(
            "() => Object.keys(window).filter("
            "k => k.startsWith('__bh') || k.startsWith('__handoff'))"
        )
        assert leaked == [], f"enumerable instrumentation leaked: {leaked}"
    finally:
        cleanup()
