# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "ccauth @ git+https://github.com/synacktraa/ccauth.git@a1eeea8",
#   "browser-handoff @ git+https://github.com/synacktraa/browser-handoff.git"
# ]
# ///
"""
Example: Claude OAuth with ccauth + browser-handoff.

Showcase: ccauth orchestrates the OAuth flow, browser-handoff streams the
login page to the human when claude.ai redirects to /login. Chrome runs
locally via patchright (stealth) — no sandbox, no remote infra.

Flow:
  1. ccauth opens /oauth/authorize in a fresh patchright Chrome.
  2. With no session cookie, claude.ai redirects to /login. browser-handoff's
     trigger fires; the page is streamed to http://localhost:8080.
  3. The human opens that URL (own browser, phone over LAN, tunneled URL —
     wherever) and logs in. Once the page lands back on /oauth/authorize,
     the completion condition fires and control returns to the script.
  4. Script clicks "Authorize". claude.ai redirects to ccauth's local
     callback server, which captures the code and exchanges it for tokens.

Environment Variables (optional):
    DISCORD_WEBHOOK_URL: Discord webhook for handoff notifications.

CLI:
    --public-base URL   Public base URL to surface in notifications and the
                        logged stream link (e.g. an ngrok tunnel, or a
                        Daytona preview URL when running this script inside
                        a sandbox). When omitted, falls back to
                        http://<host>:<port>.

Run:
    uv run examples/claude_oauth_login_handoff/local.py
    uv run examples/claude_oauth_login_handoff/local.py --public-base https://abc.daytona.preview
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import tempfile
from typing import Any

from ccauth import run_auth_custom
from ccauth.modes import CallbackServer
from patchright.async_api import async_playwright

from browser_handoff import Handoff, Scenario, ServerConfig
from browser_handoff.detection import Detection
from browser_handoff.notifiers import DiscordNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

STREAMING_PORT = 8080


async def run_claude_oauth(public_base: str | None = None) -> dict[str, Any]:
    async def capture_code(authorize_url: str, server: CallbackServer) -> str:
        notifiers = []
        if webhook := os.getenv("DISCORD_WEBHOOK_URL"):
            notifiers.append(
                DiscordNotifier(webhook_url=webhook, username="ccauth Handoff")
            )

        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="Claude Login",
                    trigger=Detection.url(path_contains=["/login"]),
                    complete=Detection.url(path_contains=["/oauth/authorize"]),
                ),
            ],
            # 0.0.0.0 so the stream is reachable from a phone over LAN, an
            # ngrok tunnel, or a Daytona preview URL — `public_base`, when
            # set, replaces what gets printed in logs and pushed to notifiers.
            server=ServerConfig(
                host="0.0.0.0",
                port=STREAMING_PORT,
                public_base=public_base,
            ),
            notifiers=notifiers,
        )

        with tempfile.TemporaryDirectory(prefix="ccauth_handoff_") as profile_dir:
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    channel="chrome",
                    headless=False,  # patchright stealth needs headed Chrome
                    no_viewport=True,
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto(authorize_url, wait_until="domcontentloaded")

                    # Fresh profile → claude.ai redirects /oauth/authorize → /login.
                    # handoff.run catches the redirect via framenavigated, streams
                    # the page to the human, and returns once they land back on
                    # /oauth/authorize. If already logged in, returns immediately.
                    result = await handoff.run(page, timeout=30)
                    if result.was_blocked:
                        if result.timed_out:
                            raise TimeoutError(
                                f"Human did not finish login within "
                                f"{handoff.server.completion_timeout:.0f}s"
                            )
                        logger.info("Human completed: %s", result.scenario_name)

                    # The Authorize button may be briefly disabled while
                    # the page finishes settling — wait for it to be visible.
                    btn = page.get_by_role("button", name="Authorize", exact=True).first
                    await btn.wait_for(state="visible", timeout=60_000)
                    await btn.click()

                    # claude.ai redirects to localhost:<port>/callback?code=...
                    # ccauth's CallbackServer (passed in by run_auth_custom) is
                    # already listening on that port — wait_for_code() blocks
                    # until the redirect arrives, then returns the code.
                    code = await asyncio.to_thread(server.wait_for_code, 300)
                    return code
                finally:
                    await ctx.close()

    return await run_auth_custom(capture_code)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--public-base",
        default=None,
        help="Public base URL the human will use to reach the stream "
        "(e.g. a tunnel or sandbox preview URL).",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    result = await run_claude_oauth(public_base=args.public_base)
    logger.info("OAuth successful")
    logger.info("Access token: %s...", result["claudeAiOauth"]["accessToken"][:20])
    logger.info("Scopes: %s", result["claudeAiOauth"]["scopes"])


if __name__ == "__main__":
    asyncio.run(main())
