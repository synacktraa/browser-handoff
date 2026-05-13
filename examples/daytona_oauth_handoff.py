#!/usr/bin/env python3
"""
Example: Claude OAuth with browser-handoff in Daytona Sandbox.

This example demonstrates using browser-handoff for human-in-the-loop
OAuth authentication running inside a Daytona sandbox.

Features:
- Declarative image building with Patchright/Chromium
- Persistent volume for browser profile (maintains login sessions)
- CDP-based remote browser connection
- Clean async context manager API

Requirements:
    pip install daytona playwright browser-handoff

Environment Variables:
    DAYTONA_API_KEY: Your Daytona API key
    DAYTONA_TARGET: Target region (us, eu)
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from daytona import AsyncSandbox
    from playwright.async_api import BrowserContext, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _build_browser_image():
    """Build Daytona image with browser dependencies."""
    from daytona import Image

    return (
        Image.debian_slim("3.12")
        .run_commands(
            # Install display server and browser dependencies
            "apt-get update",
            "apt-get install -y --no-install-recommends "
            "xvfb xauth "
            "libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 "
            "libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 "
            "libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 "
            "libatspi2.0-0 libgtk-3-0",
            "rm -rf /var/lib/apt/lists/*",
        )
        .pip_install("patchright")
        .run_commands("patchright install chromium")
    )


# Browser launcher script that runs inside the sandbox
_BROWSER_LAUNCHER = textwrap.dedent('''
    import asyncio
    from patchright.async_api import async_playwright

    async def main():
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir="{profile_path}",
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--remote-debugging-port=9222",
                    "--remote-debugging-address=0.0.0.0",
                ],
            )
            # Keep browser running until process is killed
            while True:
                await asyncio.sleep(1)

    asyncio.run(main())
''')

# Volume for persistent browser profile
PROFILE_VOLUME_ID = "browser-handoff-profile"
PROFILE_MOUNT_PATH = "/home/daytona/.browser-profile"
CDP_PORT = 9222


class SandboxBrowser:
    """Browser wrapper providing access to context and page.

    Attributes:
        context: Browser context connected via CDP
        page: Default page for navigation
    """

    def __init__(self, context: "BrowserContext", page: "Page"):
        self._context = context
        self._page = page

    @property
    def context(self) -> "BrowserContext":
        """Browser context connected via CDP."""
        return self._context

    @property
    def page(self) -> "Page":
        """Default page for navigation."""
        return self._page


class BrowserEnabledSandbox:
    """Sandbox wrapper with browser support.

    Provides access to both the Daytona sandbox and a browser
    instance running inside it via CDP.

    Attributes:
        browser: SandboxBrowser with .context and .page access
    """

    def __init__(self, sandbox: "AsyncSandbox", browser: SandboxBrowser):
        self._sandbox = sandbox
        self._browser = browser

    @property
    def browser(self) -> SandboxBrowser:
        """Browser with .context and .page access."""
        return self._browser

    def __getattr__(self, name: str) -> Any:
        """Delegate all other attributes to the underlying sandbox."""
        return getattr(self._sandbox, name)


async def _get_cdp_ws_url(base_url: str) -> str:
    """Get WebSocket URL from Chrome's /json/version endpoint."""
    import aiohttp

    # Handle both http and https URLs
    version_url = f"{base_url}/json/version"

    async with aiohttp.ClientSession() as session:
        async with session.get(version_url) as resp:
            data = await resp.json()
            return data["webSocketDebuggerUrl"]


@asynccontextmanager
async def create_browser_enabled_sandbox() -> AsyncIterator[BrowserEnabledSandbox]:
    """Create a Daytona sandbox with browser support for human-in-the-loop tasks.

    This context manager:
    1. Creates a sandbox with Patchright/Chromium pre-installed
    2. Mounts a persistent volume for browser profile data
    3. Launches browser inside sandbox with CDP exposed
    4. Connects to browser via CDP from local machine
    5. Yields a BrowserEnabledSandbox with .browser.context and .browser.page
    6. Cleans up browser and sandbox on exit

    The persistent volume maintains browser state (cookies, localStorage, etc.)
    across sandbox runs, reducing repeated login prompts.

    Yields:
        BrowserEnabledSandbox with browser access and all sandbox methods.

    Example:
        async with create_browser_enabled_sandbox() as sandbox:
            await sandbox.browser.page.goto("https://example.com")

            # Use browser-handoff for human intervention
            result = await handoff.wait_if_blocked(
                sandbox.browser.page, sandbox.browser.context
            )

            # Access sandbox methods directly
            response = await sandbox.process.exec("echo 'Hello'")
    """
    from daytona import (
        AsyncDaytona,
        CreateSandboxFromImageParams,
        Resources,
        VolumeMount,
    )
    from playwright.async_api import async_playwright

    async with AsyncDaytona() as daytona:
        # Get or create persistent volume for browser profile
        volume = await daytona.volume.get(PROFILE_VOLUME_ID, create=True)
        logger.info("Using volume: %s", volume.id)

        # Create sandbox with browser image and volume
        sandbox = await daytona.create(
            CreateSandboxFromImageParams(
                image=_build_browser_image(),
                resources=Resources(cpu=2, memory=4, disk=10),
                volumes=[
                    VolumeMount(
                        volume_id=volume.id,
                        mount_path=PROFILE_MOUNT_PATH,
                    )
                ],
            ),
            timeout=300,  # Image build may take time on first run
        )
        logger.info("Sandbox created: %s", sandbox.id)

        try:
            # Upload and run browser launcher script inside sandbox
            launcher_code = _BROWSER_LAUNCHER.format(profile_path=PROFILE_MOUNT_PATH)
            launcher_path = "/tmp/browser_launcher.py"
            await sandbox.fs.upload_file(
                launcher_code.encode(), launcher_path
            )

            # Start browser in background with xvfb
            await sandbox.process.exec(
                f"nohup xvfb-run -a python {launcher_path} > /tmp/browser.log 2>&1 &",
            )
            logger.info("Browser launcher started in sandbox")

            # Wait for browser to be ready
            await asyncio.sleep(3)

            # Get preview URL for CDP port
            preview = await sandbox.get_preview_link(CDP_PORT, expires_in=3600)
            cdp_base_url = f"https://{preview.url}"
            logger.info("CDP preview URL: %s", cdp_base_url)

            # Get WebSocket URL and connect via CDP
            ws_url = await _get_cdp_ws_url(cdp_base_url)
            # Replace the host in ws_url with preview URL
            ws_url = ws_url.replace("ws://localhost:9222", f"wss://{preview.url}")
            ws_url = ws_url.replace("ws://127.0.0.1:9222", f"wss://{preview.url}")
            logger.info("Connecting to CDP: %s", ws_url)

            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(ws_url)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()

                logger.info("Connected to browser via CDP")

                yield BrowserEnabledSandbox(
                    sandbox=sandbox,
                    browser=SandboxBrowser(context=context, page=page),
                )

        finally:
            await sandbox.delete()
            logger.info("Sandbox deleted")


async def run_claude_oauth(authorize_url: str) -> dict:
    """Run Claude OAuth flow with human-in-the-loop handoff.

    Args:
        authorize_url: The OAuth authorization URL.

    Returns:
        Dict with OAuth code and state from callback.
    """
    from urllib.parse import parse_qs, urlparse

    from browser_handoff import Detection, Handoff, Scenario

    # Configure handoff for Claude OAuth flow
    handoff = Handoff(
        scenarios=[
            Scenario(
                name="Claude Login",
                trigger=Detection.url(path_contains=["/login"]),
                complete=Detection.url(path_contains=["/oauth/authorize"]),
            ),
        ],
    )

    async with create_browser_enabled_sandbox() as sandbox:
        page = sandbox.browser.page
        context = sandbox.browser.context

        logger.info("Navigating to: %s", authorize_url)
        await page.goto(authorize_url, wait_until="domcontentloaded")

        # Check if human intervention needed (login required)
        result = await handoff.wait_if_blocked(page, context, trigger_timeout=10)

        if result.was_blocked:
            logger.info("Human completed: %s", result.scenario_name)

        # Click authorize button (bot action after human login)
        try:
            authorize_btn = page.get_by_role("button", name="Authorize")
            await authorize_btn.wait_for(state="visible", timeout=10000)
            await authorize_btn.click()
            await page.wait_for_url("**/callback**", timeout=15000)
        except Exception as e:
            logger.warning("Authorize click failed: %s", e)

        # Extract callback URL parameters
        final_url = page.url
        logger.info("Final URL: %s", final_url)

        parsed = urlparse(final_url)
        params = parse_qs(parsed.query)

        return {
            "code": params.get("code", [None])[0],
            "state": params.get("state", [None])[0],
        }


async def main():
    """Demo the OAuth flow."""
    # Example OAuth URL - replace with actual values
    authorize_url = (
        "https://claude.ai/oauth/authorize?"
        "client_id=YOUR_CLIENT_ID&"
        "response_type=code&"
        "redirect_uri=http://localhost:8080/callback&"
        "scope=user:profile"
    )

    try:
        result = await run_claude_oauth(authorize_url)
        logger.info("OAuth result: %s", result)
    except Exception as e:
        logger.error("OAuth failed: %s", e)
        raise


if __name__ == "__main__":
    asyncio.run(main())
