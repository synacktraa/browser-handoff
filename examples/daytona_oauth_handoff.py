# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "daytona",
#   "patchright",
#   "browser-handoff",
#   "ccauth",
# ]
# ///
"""
Example: Claude OAuth with browser-handoff in Daytona Sandbox.

This example demonstrates using browser-handoff for human-in-the-loop
OAuth authentication running inside a Daytona sandbox.

Features:
- Daytona sandbox with persistent browser profile
- CDP-based remote browser connection
- browser-handoff streaming for human login intervention
- ccauth for OAuth token exchange

Requirements:
    pip install daytona patchright browser-handoff ccauth

Environment Variables:
    DAYTONA_API_KEY: Your Daytona API key
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from daytona import AsyncSandbox
    from playwright.async_api import Browser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================================
# Daytona Sandbox with Browser Support
# =============================================================================

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
            while True:
                await asyncio.sleep(1)

    asyncio.run(main())
''')

PROFILE_VOLUME_ID = "browser-handoff-profile"
PROFILE_MOUNT_PATH = "/home/daytona/.browser-profile"
CDP_PORT = 9222
STREAMING_PORT = 8080


def _build_browser_image():
    """Build Daytona image with browser dependencies."""
    from daytona import Image

    return (
        Image.debian_slim("3.12")
        .run_commands(
            "apt-get update && "
            "apt-get install -y --no-install-recommends xvfb xauth && "
            "rm -rf /var/lib/apt/lists/*",
        )
        .pip_install("patchright")
        .run_commands("patchright install chromium")
    )


class BrowserEnabledSandbox:
    """Sandbox wrapper with browser support via CDP."""

    def __init__(self, sandbox: "AsyncSandbox", browser: "Browser"):
        self._sandbox = sandbox
        self._browser = browser

    @property
    def sandbox(self) -> "AsyncSandbox":
        return self._sandbox

    @property
    def browser(self) -> "Browser":
        return self._browser


async def _get_cdp_ws_url(base_url: str) -> str:
    """Get WebSocket URL from Chrome's /json/version endpoint."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}/json/version") as resp:
            data = await resp.json()
            return data["webSocketDebuggerUrl"]


@asynccontextmanager
async def create_browser_enabled_sandbox() -> AsyncIterator[BrowserEnabledSandbox]:
    """Create a Daytona sandbox with browser support.

    Creates a sandbox with:
    - Patchright/Chromium pre-installed
    - Persistent volume for browser profile
    - CDP exposed for remote connection

    Example:
        async with create_browser_enabled_sandbox() as ctx:
            context = ctx.browser.contexts[0]
            page = context.pages[0]
            await page.goto("https://example.com")
    """
    from daytona import (
        AsyncDaytona,
        CreateSandboxFromImageParams,
        Resources,
        VolumeMount,
    )
    from playwright.async_api import async_playwright

    async with AsyncDaytona() as daytona:
        volume = await daytona.volume.get(PROFILE_VOLUME_ID, create=True)
        logger.info("Using volume: %s", volume.id)

        sandbox = await daytona.create(
            CreateSandboxFromImageParams(
                image=_build_browser_image(),
                resources=Resources(cpu=2, memory=4, disk=10),
                volumes=[VolumeMount(volume_id=volume.id, mount_path=PROFILE_MOUNT_PATH)],
            ),
            timeout=300,
        )
        logger.info("Sandbox created: %s", sandbox.id)

        try:
            # Upload and run browser launcher
            launcher_code = _BROWSER_LAUNCHER.format(profile_path=PROFILE_MOUNT_PATH)
            await sandbox.fs.upload_file(launcher_code.encode(), "/tmp/browser_launcher.py")
            await sandbox.process.exec(
                "nohup xvfb-run -a python /tmp/browser_launcher.py > /tmp/browser.log 2>&1 &"
            )
            logger.info("Browser started in sandbox")
            await asyncio.sleep(3)

            # Connect via CDP
            preview = await sandbox.get_preview_link(CDP_PORT)
            cdp_base_url = f"https://{preview.url}"
            ws_url = await _get_cdp_ws_url(cdp_base_url)
            ws_url = ws_url.replace("ws://localhost:9222", f"wss://{preview.url}")
            ws_url = ws_url.replace("ws://127.0.0.1:9222", f"wss://{preview.url}")

            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(ws_url)
                logger.info("Connected to browser via CDP")
                yield BrowserEnabledSandbox(sandbox=sandbox, browser=browser)

        finally:
            await sandbox.delete()
            logger.info("Sandbox deleted")


# =============================================================================
# Claude OAuth Flow
# =============================================================================


async def run_claude_oauth() -> dict[str, Any]:
    """Run Claude OAuth with browser-handoff for human login.

    Uses:
    - ccauth for OAuth primitives (PKCE, callback server, token exchange)
    - browser-handoff for human-in-the-loop login streaming
    - Daytona sandbox for isolated browser environment
    """
    from ccauth.modes._callback import start_callback_server
    from ccauth.oauth import build_authorize_url, exchange_code, generate_pkce, generate_state
    from ccauth.runner import (
        AUTHORIZE_URL,
        CALLBACK_PATH,
        CLIENT_ID,
        SCOPE,
        USER_AGENT,
    )

    from browser_handoff import Detection, Handoff, Scenario, ServerConfig

    # Setup OAuth with ccauth utilities
    pkce = generate_pkce()
    state = generate_state()
    callback_server = start_callback_server(expected_state=state, callback_path=CALLBACK_PATH)
    redirect_uri = f"http://localhost:{callback_server.port}{CALLBACK_PATH}"

    authorize_url = build_authorize_url(
        authorize_url=AUTHORIZE_URL,
        client_id=CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=SCOPE,
        code_challenge=pkce.challenge,
        state=state,
    )
    logger.info("Callback server on port %d", callback_server.port)

    async with create_browser_enabled_sandbox() as ctx:
        context = ctx.browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # Get public URL for browser-handoff streaming
        streaming_preview = await ctx.sandbox.create_signed_preview_url(
            STREAMING_PORT, expires_in_seconds=3600
        )
        public_base = f"https://{streaming_preview.url}"

        # Configure browser-handoff
        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="Claude Login",
                    trigger=Detection.url(path_contains=["/login"]),
                    complete=Detection.url(path_contains=["/oauth/authorize"]),
                ),
            ],
            server=ServerConfig(port=STREAMING_PORT, public_base=public_base),
        )

        # Navigate and handle login
        await page.goto(authorize_url, wait_until="domcontentloaded")
        result = await handoff.wait_if_blocked(page, context, trigger_timeout=10)

        if result.was_blocked:
            logger.info("Human completed: %s", result.scenario_name)

        # Click Authorize (wait for Cloudflare Turnstile)
        btn = page.get_by_role("button", name="Authorize", exact=True).first
        await btn.wait_for(state="visible", timeout=60000)
        await btn.click()
        await page.wait_for_url(f"**/localhost:{callback_server.port}/**", timeout=15000)

    # Exchange code for tokens using ccauth
    code = callback_server.wait_for_code(timeout=30.0)
    tokens = exchange_code(
        token_url="https://api.anthropic.com/oauth/token",
        client_id=CLIENT_ID,
        code=code,
        code_verifier=pkce.verifier,
        redirect_uri=redirect_uri,
        state=state,
        user_agent=USER_AGENT,
    )

    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at_ms": tokens.expires_at_ms,
        "scopes": tokens.scopes,
    }


async def main():
    """Run the OAuth flow."""
    result = await run_claude_oauth()
    logger.info("OAuth successful!")
    logger.info("Access token: %s...", result["access_token"][:20])
    logger.info("Scopes: %s", result["scopes"])


if __name__ == "__main__":
    asyncio.run(main())
