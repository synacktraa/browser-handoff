"""Integration tests for LLMDetection's activity-debounced watch loop.

Runs against real Chromium but makes NO LLM calls — the callback is a stub, so
these validate the *injected* activity listeners and the debounce/cleanup
behavior independent of any model or API key. They prove the injected JS
actually fires in a real browser; the unit tests in test_detection.py cover
the decision logic and loop wiring against a fake page.

Listener set covers the operator-input channels: mousedown, keydown, wheel,
scroll, touchstart, input, paste. Page-driven DOM mutations are deliberately
NOT a signal here — on real sites they fire constantly (carousels, ads,
analytics) and would burn vision calls against a still operator.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from browser_handoff.detection.llm import LLMDetection


async def test_dom_mutation_alone_does_not_trigger_check(
    page: Page, base_url: str
) -> None:
    # Page-driven mutations must not look like operator activity — the
    # whole point of dropping the MutationObserver. Inject a mutation
    # via page.evaluate (no operator involvement, no input event) and
    # confirm the watch loop stays quiet.
    await page.goto(f"{base_url}/dynamic")

    det = LLMDetection(condition="unused", idle_seconds=0.2, max_interval=0.0)
    det._poll_interval = 0.05
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = det.register_listeners(page, cb)
    try:
        await asyncio.sleep(0.4)
        assert calls == [], "should not check on a quiet page"

        # Programmatic DOM mutation (no operator event). Old watcher
        # would have ticked here; new one must not.
        await page.evaluate(
            "() => { const d = document.createElement('div');"
            " d.textContent = 'ad'; document.body.appendChild(d); }"
        )
        await asyncio.sleep(0.6)
        assert calls == [], (
            "page-driven DOM mutation must not register as operator activity"
        )
    finally:
        cleanup()


async def test_user_input_triggers_one_debounced_check(
    page: Page, base_url: str
) -> None:
    # /login has a text field. Typing fires keydown + input — both in the
    # listener set — so a single debounced check should land. Test the
    # input-side path of the unified watcher.
    await page.goto(f"{base_url}/login")

    det = LLMDetection(condition="unused", idle_seconds=0.3, max_interval=0.0)
    det._poll_interval = 0.05
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = det.register_listeners(page, cb)
    try:
        # No interaction yet → no checks.
        await asyncio.sleep(0.6)
        assert calls == [], "should not check without any activity"

        # Operator focuses the field and types — fires pointerdown + keydown +
        # input, all caught by the injected window listeners. Typing is fast
        # (no per-key delay), so it settles well within idle_seconds and yields
        # a single debounced check.
        await page.click("input[type=email]")
        await page.keyboard.type("operator@example.com")
        await asyncio.sleep(0.6)
        assert len(calls) == 1, "user input should trigger one debounced check"

        # Quiet again → debounced, no further checks.
        await asyncio.sleep(0.6)
        assert len(calls) == 1
    finally:
        cleanup()

    # After cleanup the loop is gone: more typing must not produce checks.
    snapshot = len(calls)
    await page.keyboard.type("more")
    await asyncio.sleep(0.6)
    assert len(calls) == snapshot, "no checks after cleanup"
