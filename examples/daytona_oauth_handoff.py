# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "daytona",
#   "patchright",
#   "ccauth @ git+https://github.com/synacktraa/ccauth.git",
#   "browser-handoff @ git+https://github.com/synacktraa/browser-handoff.git@agent/storm-frost-0xeh"
# ]
# ///
"""
Example: Claude OAuth with browser-handoff in Daytona Sandbox.

Demonstrates human-in-the-loop OAuth using:
- Daytona sandbox with persistent browser profile
- browser-handoff for streaming login to human
- ccauth for OAuth orchestration

Environment Variables:
    DAYTONA_API_KEY: Your Daytona API key
    DISCORD_WEBHOOK_URL: Your Discord webhook URL
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator
from urllib.parse import parse_qs, urlparse

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
    from daytona import Image

    return (
        Image.debian_slim("3.12")
        .run_commands(
            "apt-get update && "
            "apt-get install -y --no-install-recommends "
            "xvfb xauth x11vnc novnc xfce4 xfce4-terminal dbus-x11 && "
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
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}/json/version") as resp:
            data = await resp.json()
            return data["webSocketDebuggerUrl"]


import asyncio
import time

VOLUME_READY_TIMEOUT_MS = 30000
VOLUME_POLL_INTERVAL_MS = 500

@asynccontextmanager
async def create_browser_enabled_sandbox() -> AsyncIterator[BrowserEnabledSandbox]:
    """Create a Daytona sandbox with browser support."""
    from daytona import AsyncDaytona, CreateSandboxFromImageParams, Resources, VolumeMount
    from playwright.async_api import async_playwright

    async with AsyncDaytona() as daytona:
        volume = await daytona.volume.get(PROFILE_VOLUME_ID, create=True)

        volume_deadline = time.time() + (VOLUME_READY_TIMEOUT_MS / 1000)
        while volume.state != "ready" and time.time() < volume_deadline:
            await asyncio.sleep(VOLUME_POLL_INTERVAL_MS / 1000)
            volume = await daytona.volume.get(PROFILE_VOLUME_ID, create=False)

        if volume.state != "ready":
            raise RuntimeError(
                f"Volume '{PROFILE_VOLUME_ID}' not ready after {VOLUME_READY_TIMEOUT_MS}ms (state: {volume.state})"
            )

        logger.info("Using volume: %s", volume.id)

        sandbox = await daytona.create(
            CreateSandboxFromImageParams(
                image=_build_browser_image(),
                resources=Resources(cpu=2, memory=4, disk=10),
                volumes=[VolumeMount(volume_id=volume.id, mount_path=PROFILE_MOUNT_PATH)],
            ),
            timeout=300,
            on_snapshot_create_logs=lambda log: logger.info(f"Sandbox snapshot log: {log}")
        )
        logger.info("Sandbox created: %s", sandbox.id)

        try:
            launcher_code = _BROWSER_LAUNCHER.format(profile_path=PROFILE_MOUNT_PATH)
            await sandbox.fs.upload_file(launcher_code.encode(), "/tmp/browser_launcher.py")
            await sandbox.process.exec(
                "nohup xvfb-run -a python /tmp/browser_launcher.py > /tmp/browser.log 2>&1 &"
            )
            logger.info("Browser started in sandbox")
            await asyncio.sleep(3)

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
    """Run Claude OAuth with browser-handoff for human login."""
    from ccauth import run_auth_custom
    from ccauth.modes import CallbackServer

    from browser_handoff import Detection, Handoff, Scenario, ServerConfig, DiscordNotifier

    async def capture_code(authorize_url: str, server: CallbackServer) -> str:
        # Close server since we won't receive the callback in the sandbox, but detect via URL change instead
        server.close() 

        async with create_browser_enabled_sandbox() as ctx:
            context = ctx.browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            # Get public URL for browser-handoff streaming
            streaming_preview = await ctx.sandbox.create_signed_preview_url(
                STREAMING_PORT, expires_in_seconds=3600
            )

            # Configure browser-handoff
            handoff = Handoff(
                scenarios=[
                    Scenario(
                        name="Claude Login",
                        trigger=Detection.url(path_contains=["/login"]),
                        complete=Detection.url(path_contains=["/oauth/authorize"]),
                    ),
                ],
                server=ServerConfig(
                    port=STREAMING_PORT,
                    public_base=f"https://{streaming_preview.url}",
                ),
                notifiers=[
                    DiscordNotifier(
                        webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
                        username="CCAuth Handoff Bot",
                    )
                ],
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

            # Wait for redirect to callback URL
            # Note: Page won't load (sandbox can't reach localhost), but URL changes
            await page.wait_for_url(f"**{server.redirect_uri}**", timeout=15000)

            # Extract code from browser URL
            parsed = urlparse(page.url)
            code = parse_qs(parsed.query)["code"][0]

        return code

    return await run_auth_custom(capture_code)


async def main():
    result = await run_claude_oauth()
    logger.info("OAuth successful!")
    logger.info("Access token: %s...", result["claudeAiOauth"]["accessToken"][:20])
    logger.info("Scopes: %s", result["claudeAiOauth"]["scopes"])


if __name__ == "__main__":
    asyncio.run(main())
