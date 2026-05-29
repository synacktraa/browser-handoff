"""Integration tests for LLMDetection's activity-debounced watch loop.

Runs against real Chromium but makes NO LLM calls — the callback is a stub, so
these validate the *injected* activity tracking (the MutationObserver and the
human-input listeners) and the debounce/cleanup behavior independent of any
model or API key. They're what prove the injected JS actually fires in a real
browser; the unit tests in test_detection.py cover the decision logic and loop
wiring against a fake page.

One activity source per test, exercised in isolation:
  - test_dom_mutation_... drives the MutationObserver (a DOM change, no input).
  - test_user_input_... drives the input listeners (typing into a field changes
    its value property but not the DOM, so only the input path can fire).
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from browser_handoff.detection.llm import LLMDetection


async def test_dom_mutation_triggers_one_debounced_check(
    page: Page, base_url: str
) -> None:
    await page.goto(f"{base_url}/dynamic")

    det = LLMDetection(condition="unused", idle_seconds=0.3, max_interval=0.0)
    det._poll_interval = 0.05
    calls: list = []

    async def cb(d):  # type: ignore[no-untyped-def]
        calls.append(d)

    cleanup = det.register_listeners(page, cb)
    try:
        # Static page, no interaction → no checks at all.
        await asyncio.sleep(0.6)
        assert calls == [], "should not check without any activity"

        # Mutate the DOM — the injected MutationObserver marks activity, and a
        # single check fires once it settles past idle_seconds.
        await page.click("#add")
        await asyncio.sleep(0.6)
        assert len(calls) == 1, "DOM mutation should trigger one debounced check"

        # Quiet again → debounced, no further checks.
        await asyncio.sleep(0.6)
        assert len(calls) == 1
    finally:
        cleanup()

    # After cleanup the loop is gone: more activity must not produce checks.
    snapshot = len(calls)
    await page.click("#add")
    await asyncio.sleep(0.6)
    assert len(calls) == snapshot, "no checks after cleanup"


async def test_user_input_triggers_one_debounced_check(
    page: Page, base_url: str
) -> None:
    # /login has a text field. Typing into it changes the input's value
    # property but not the DOM, so the MutationObserver stays silent — any
    # check here must come from the injected pointerdown/keydown/input
    # listeners. This is the input-side counterpart to the DOM-mutation test.
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
