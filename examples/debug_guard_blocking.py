#!/usr/bin/env python3
"""
Example: Verify guard() blocking behavior.

This script demonstrates that the guard() context manager properly blocks
until human intervention is complete before yielding to user code.

Expected behavior:
1. Guard detects trigger condition (URL matches "/")
2. Streaming server starts
3. Guard waits for human to complete (or timeout)
4. Only AFTER completion/timeout does the user code run

Run with: python examples/debug_guard_blocking.py
"""

import asyncio
import logging

from playwright.async_api import async_playwright

from browser_handoff import Detection, Handoff, Scenario

# Enable logging to see internal flow
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    """Test that guard() blocks properly when a trigger is detected."""

    # Create a handoff that triggers on any URL (will trigger immediately)
    handoff = Handoff(
        scenarios=[
            Scenario(
                name="test_trigger",
                trigger=Detection.url(path_contains=["/"]),  # Matches any path
                complete=Detection.url(path_contains=["/complete"]),  # Will timeout
            ),
        ],
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Navigate to a simple page
        await page.goto("https://example.com")
        logger.info("Page loaded at: %s", page.url)

        logger.info("=" * 70)
        logger.info("ENTERING GUARD CONTEXT")
        logger.info("If blocking works, this waits for human intervention")
        logger.info("=" * 70)

        try:
            # Use a short timeout for testing
            handoff.server.timeout = 10  # 10 second timeout

            async with handoff.guard(page) as session:
                # This code should NOT run until after human completes
                # intervention (or timeout occurs)
                logger.info("=" * 70)
                logger.info("INSIDE GUARD CONTEXT - USER CODE RUNNING")
                logger.info(
                    "Session state: was_blocked=%s, scenario=%s",
                    session.was_blocked,
                    session.scenario_name,
                )
                logger.info("=" * 70)

                # Simulate bot logic
                logger.info("About to run bot logic...")
                await asyncio.sleep(0.1)
                logger.info("Bot logic completed")

        except Exception as e:
            logger.info("Guard raised exception (expected for timeout): %s", e)

        await browser.close()

    logger.info("Script completed")


if __name__ == "__main__":
    asyncio.run(main())
