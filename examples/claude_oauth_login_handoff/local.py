# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "ccauth @ git+https://github.com/synacktraa/ccauth.git@a1eeea8",
#   "browser-handoff @ git+https://github.com/synacktraa/browser-handoff.git",
#   "rich"
# ]
# ///
"""
Example: Claude OAuth with ccauth + browser-handoff.

Showcase: ccauth orchestrates the OAuth flow, browser-handoff streams the
login page to the human when claude.ai redirects to /login. Chrome runs
locally via patchright (stealth) — no sandbox, no remote infra.

CLI:
    --public-base URL   Public base URL to surface in the rich panel and
                        in notifier messages (e.g. an ngrok tunnel, or a
                        Daytona preview URL when running this script inside
                        a sandbox). When omitted, falls back to
                        http://<host>:<port>.

Environment Variables (optional):
    DISCORD_WEBHOOK_URL: Discord webhook for handoff notifications.

Run:
    uv run examples/claude_oauth_login_handoff/local.py
    uv run examples/claude_oauth_login_handoff/local.py --public-base https://abc.daytona.preview
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import tempfile
from typing import Any

from ccauth import run_auth_custom
from ccauth.modes import CallbackServer
from patchright.async_api import async_playwright
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from browser_handoff import Handoff, Scenario, ServerConfig
from browser_handoff.detection import Detection
from browser_handoff.notifiers import DiscordNotifier, Notifier

# Silence the library's INFO chatter (mouse events, framenavigated, screencast
# frames). Rich panels surface every milestone the operator actually needs.
logging.basicConfig(level=logging.WARNING)

# force_terminal=True so ANSI escapes still get emitted when stdout is a pipe
# (which is exactly what happens inside a Daytona sandbox). The pipe receiver
# — your local terminal, via `in_daytona.py`'s tail — renders them as colors.
console = Console(force_terminal=True)

STREAMING_PORT = 8080


def _mask(value: str, keep: int = 14) -> str:
    """Show first `keep` chars then an ellipsis — enough to identify a token
    by prefix without leaking the secret bit."""
    s = str(value)
    if len(s) <= keep:
        return s
    return f"{s[:keep]}…"


def _format_value(key: str, value: Any) -> str:
    lk = key.lower()
    if "token" in lk or "secret" in lk or "key" in lk:
        return _mask(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    # Epoch-ms timestamps are far more readable as an ISO datetime — keep
    # the raw value visible too so it's still copy-pasteable.
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 1e12
        and ("expires" in lk or lk.endswith("at"))
    ):
        try:
            dt = datetime.datetime.fromtimestamp(value / 1000, datetime.UTC)
            return f"{value} ({dt.isoformat()})"
        except Exception:
            pass
    return str(value)


def _credentials_panel(result: dict[str, Any]) -> Panel:
    inner = result.get("claudeAiOauth", result)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column()
    for key, value in inner.items():
        table.add_row(key, _format_value(key, value))
    return Panel(
        table,
        title="[bold green]✓ OAuth captured[/bold green]",
        border_style="green",
        padding=(1, 2),
    )


async def run_claude_oauth(public_base: str | None = None) -> dict[str, Any]:
    async def capture_code(authorize_url: str, server: CallbackServer) -> str:
        # If DISCORD_WEBHOOK_URL is set, use it; otherwise leave the list
        # empty and browser-handoff will fall back to its built-in
        # ConsoleNotifier (rich panel with the stream URL).
        notifiers: list[Notifier] = []
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
                console.print("[cyan]→[/cyan] Launching Chrome (patchright stealth)")
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    channel="chrome",
                    headless=False,  # patchright stealth needs headed Chrome
                    no_viewport=True,
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    console.print("[cyan]→[/cyan] Opening authorize URL")
                    await page.goto(authorize_url, wait_until="domcontentloaded")

                    # Fresh profile → claude.ai redirects /oauth/authorize → /login.
                    # handoff.run catches the redirect via framenavigated, fires
                    # the notifiers (rich panel + Discord), and returns once the
                    # human lands back on /oauth/authorize.
                    result = await handoff.run(page, timeout=30)
                    if result.was_blocked:
                        if result.timed_out:
                            console.print(
                                "[red]✗ Human did not finish login in time[/red]"
                            )
                            raise TimeoutError(
                                f"Human did not finish login within "
                                f"{handoff.server.completion_timeout:.0f}s"
                            )
                        console.print(
                            f"[green]✓[/green] Login completed "
                            f"(took {result.duration:.1f}s)"
                        )

                    console.print("[cyan]→[/cyan] Clicking Authorize")
                    btn = page.get_by_role(
                        "button", name="Authorize", exact=True
                    ).first
                    await btn.wait_for(state="visible", timeout=60_000)
                    await btn.click()

                    # claude.ai redirects to localhost:<port>/callback?code=...
                    # ccauth's CallbackServer is already listening on that port —
                    # wait_for_code() blocks until the redirect arrives.
                    code = await asyncio.to_thread(server.wait_for_code, 300)
                    console.print("[green]✓[/green] OAuth callback captured")
                    return code
                finally:
                    await ctx.close()

    return await run_auth_custom(capture_code)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Claude OAuth via ccauth + browser-handoff"
    )
    parser.add_argument(
        "--public-base",
        default=None,
        help="Public base URL the human will use to reach the stream "
        "(e.g. a tunnel or sandbox preview URL).",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    console.rule("[bold]Claude OAuth — browser-handoff demo[/bold]")
    result = await run_claude_oauth(public_base=args.public_base)
    console.print()
    console.print(_credentials_panel(result))


if __name__ == "__main__":
    asyncio.run(main())
