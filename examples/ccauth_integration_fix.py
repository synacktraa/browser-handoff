"""
Fix for ccauth integration: Correct handoff usage pattern.

The issue: Using guard() context manager around bot code that needs to run
to trigger the completion condition creates a deadlock.

Solution: Use wait_if_blocked() which checks conditions and waits only if needed,
then lets your bot code run after human intervention completes.
"""

import logging
import re
from pathlib import Path

from browser_handoff import Detection, DiscordNotifier, Handoff, Scenario

logger = logging.getLogger(__name__)

# Configure handoff to trigger only when human intervention is REQUIRED
handoff_config = Handoff(
    scenarios=[
        Scenario(
            name="Claude Login Form",
            # Trigger when the actual login FORM is visible
            # (not just /login URL which might auto-redirect if cookies work)
            trigger=Detection.all(
                Detection.url(path_contains=["/login"]),
                Detection.any(
                    Detection.element(selector='input[type="email"]'),
                    Detection.element(selector='input[type="password"]'),
                ),
            ),
            complete=Detection.url(path_contains=["/callback"]),
        ),
        Scenario(
            name="Turnstile Challenge",
            trigger=Detection.element(
                selector='iframe[src*="challenges.cloudflare.com"]'
            ),
            # Complete when challenge iframe disappears
            complete=Detection.not_(
                Detection.element(selector='iframe[src*="challenges.cloudflare.com"]')
            ),
        ),
    ],
    notifiers=[
        DiscordNotifier(
            webhook_url="YOUR_WEBHOOK_URL",
            username="CCAuth Handoff",
        ),
    ],
)


async def open_and_wait(
    authorize_url: str,
    server,  # CallbackServer
    cookies: list,
    *,
    process_page=None,  # Callable[[Page], Awaitable[None]]
    timeout: float = 180.0,
) -> str:
    """
    Correct flow using wait_if_blocked():
    1. Navigate to authorize URL
    2. Check if human intervention needed (login form visible, etc.)
    3. If yes, wait for human to complete
    4. Run bot action (click Authorize button)
    5. Wait for callback
    """
    from patchright.async_api import async_playwright

    PROFILE_DIR = Path.home() / ".ccauth" / "patchright-profile"
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    callback_pattern = re.compile(
        rf"localhost:{server.port}{re.escape(server.callback_path)}"
    )

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--disable-quic", "--disable-features=UseDnsHttpsSvcb"],
        )

        try:
            await context.add_cookies(cookies)
        except Exception as e:
            await context.close()
            raise Exception(f"Failed to inject cookies: {e}") from e

        page = context.pages[0] if context.pages else await context.new_page()
        logger.info("Navigating to authorize URL...")
        await page.goto(authorize_url, wait_until="domcontentloaded", timeout=30000)

        # Wait for page to settle (redirects, etc.)
        await page.wait_for_timeout(2000)

        # Check if human intervention is needed and wait if so
        result = await handoff_config.wait_if_blocked(page, context)

        if result.was_blocked:
            logger.info(
                "Human intervention completed (scenario: %s)", result.scenario_name
            )

        # Now run bot action - this happens AFTER human intervention (if any)
        if process_page is not None:
            try:
                await process_page(page)
            except Exception as e:
                captured_url = page.url
                await context.close()
                raise Exception(f"process_page failed at {captured_url}: {e}") from e

        # Wait for callback
        try:
            await page.wait_for_url(callback_pattern, timeout=15000)
        except Exception:
            pass

        await context.close()

    return server.wait_for_code(timeout=timeout)
