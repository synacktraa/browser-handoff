#!/usr/bin/env python3
"""
Example: OAuth flow with conditional human handoff.

This demonstrates the correct pattern for using browser-handoff in an OAuth flow:
1. Bot attempts the automated flow first
2. Guard monitors for conditions that REQUIRE human intervention
3. Only blocks when the bot cannot proceed

Key insight: Don't trigger on normal flow pages (like /login URL).
Instead, trigger on conditions that indicate the bot is stuck:
- Login form visible (cookies didn't work)
- CAPTCHA/challenge present
- Error messages

Run with: python examples/oauth_flow_with_handoff.py
"""

import asyncio
import logging
import re

from playwright.async_api import async_playwright

from browser_handoff import Detection, Handoff, Scenario

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Configure handoff to trigger only when human intervention is REQUIRED
handoff = Handoff(
    scenarios=[
        # Scenario 1: Login form is visible (cookies didn't work)
        Scenario(
            name="Login Required",
            trigger=Detection.any(
                Detection.element(selector='input[type="email"]'),
                Detection.element(selector='input[name="username"]'),
                Detection.element(selector='form[action*="login"]'),
            ),
            complete=Detection.any(
                Detection.url(path_contains=["/callback"]),
                Detection.url(path_contains=["/dashboard"]),
                Detection.element(selector='button[name="authorize"]'),
            ),
        ),
        # Scenario 2: Cloudflare Turnstile challenge
        Scenario(
            name="Turnstile Challenge",
            trigger=Detection.element(
                selector='iframe[src*="challenges.cloudflare.com"]'
            ),
            complete=Detection.not_(
                Detection.element(selector='iframe[src*="challenges.cloudflare.com"]')
            ),
        ),
        # Scenario 3: Rate limit or error page
        Scenario(
            name="Rate Limited",
            trigger=Detection.any(
                Detection.content(contains=["rate limit", "too many requests"]),
                Detection.content(contains=["please try again later"]),
            ),
            complete=Detection.url(path_contains=["/callback"]),
        ),
    ],
)


async def click_authorize(page) -> None:
    """Bot action: Click the Authorize button on the consent page."""
    logger.info("Looking for Authorize button...")
    btn = page.get_by_role("button", name="Authorize", exact=True).first
    await btn.wait_for(state="visible", timeout=10000)
    await btn.click()
    logger.info("Clicked Authorize button")


async def run_oauth_flow(authorize_url: str, callback_pattern: str) -> str:
    """Run OAuth flow with automatic handoff when bot gets stuck."""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        logger.info("Navigating to: %s", authorize_url)
        await page.goto(authorize_url, wait_until="domcontentloaded")

        # Use guard to monitor for conditions requiring human intervention
        # The guard only blocks if a trigger condition is detected
        async with handoff.guard(page=page, context=context) as session:
            if session.was_blocked:
                logger.info(
                    "Human intervention completed (scenario: %s)",
                    session.scenario_name,
                )

            # Now try the bot action (after any required human intervention)
            try:
                await click_authorize(page)
            except Exception as e:
                logger.warning("Bot action failed: %s", e)
                # Could trigger another handoff here if needed

            # Wait for callback
            try:
                await page.wait_for_url(re.compile(callback_pattern), timeout=30000)
                logger.info("Callback received!")
            except Exception:
                logger.warning("Timeout waiting for callback")

        final_url = page.url
        await browser.close()

    return final_url


async def main():
    """Demo the OAuth flow pattern."""
    # This is a placeholder - in real usage, you'd use actual OAuth URLs
    result = await run_oauth_flow(
        authorize_url="https://example.com/oauth/authorize",
        callback_pattern=r"example\.com/callback",
    )
    logger.info("Final URL: %s", result)


if __name__ == "__main__":
    asyncio.run(main())
