"""
Fix for ccauth integration: Correct handoff trigger conditions.

The issue: Triggering on `/login` URL blocks the bot even when cookies work
and the page auto-redirects to the consent page.

Solution: Trigger on conditions that indicate the bot CANNOT proceed:
1. Login form is visible (not just /login URL)
2. Turnstile/CAPTCHA challenge appears
3. Error messages

This file shows the corrected handoff configuration.
"""

from browser_handoff import Detection, DiscordNotifier, Handoff, Scenario

# CORRECT: Trigger on conditions that require human intervention
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
                    Detection.element(selector='button[type="submit"]'),
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

# ALTERNATIVE: Run bot first, only use handoff as fallback
# This is often the cleanest approach for OAuth flows


async def open_and_wait_v2(
    authorize_url: str,
    server,  # CallbackServer
    cookies: list,
    *,
    process_page=None,
    timeout: float = 180.0,
) -> str:
    """
    Improved flow:
    1. Try bot action first
    2. Only trigger handoff if bot fails
    """
    import re

    from patchright.async_api import async_playwright

    callback_pattern = re.compile(
        rf"localhost:{server.port}{re.escape(server.callback_path)}"
    )

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str("~/.ccauth/profile"),
            channel="chrome",
            headless=False,
        )
        await context.add_cookies(cookies)

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(authorize_url, wait_until="domcontentloaded", timeout=30000)

        # Wait a moment for redirects to settle
        await page.wait_for_timeout(2000)

        # Check if we need human intervention
        needs_handoff = False

        # Check for login form (cookies didn't work)
        if "/login" in page.url:
            try:
                login_form = page.locator('input[type="email"], input[type="password"]')
                if await login_form.count() > 0:
                    needs_handoff = True
            except Exception:
                pass

        # Check for Turnstile
        try:
            turnstile = page.locator('iframe[src*="challenges.cloudflare.com"]')
            if await turnstile.count() > 0:
                needs_handoff = True
        except Exception:
            pass

        if needs_handoff:
            # Human intervention required
            async with handoff_config.guard(page=page, context=context) as session:
                # Guard blocks until human completes
                pass

        # Now try bot action (after any human intervention)
        if process_page is not None:
            try:
                await process_page(page)
            except Exception as e:
                await context.close()
                raise Exception(f"process_page failed: {e}") from e

        # Wait for callback
        try:
            await page.wait_for_url(callback_pattern, timeout=15000)
        except Exception:
            pass

        await context.close()

    return server.wait_for_code(timeout=timeout)
