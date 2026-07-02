# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "browser-use",
#   "browser-handoff[llm] @ git+https://github.com/synacktraa/browser-handoff.git",
# ]
# ///
"""
Example: a browser-use shopping agent that asks a human for login + payment.

browser-use drives a t-shirt purchase on automationexercise.com (a test
storefront — no anti-bot, no real charge). browser-handoff is exposed
to the agent as a custom tool: when the agent gets stuck (login wall,
signup form, card entry) it calls `request_human_help` with a
natural-language `done_when` for the resume condition. The page
streams to a human and the tool returns when an LLMDetection on that
condition matches.

Architecture — one shared Chrome over CDP:
  * Playwright launches Chrome with a fixed remote-debugging port and
    holds the Page objects (browser-handoff needs a Playwright Page).
  * browser-use connects to the SAME Chrome over CDP for its agent loop.
  * `request_human_help` resolves the current Page on each call and
    awaits `h.pause(...)`.

Prereqs:
  * ANTHROPIC_API_KEY — used by both browser-use's planner and bh's
    LLMDetection.

Environment Variables (optional):
  DISCORD_WEBHOOK_URL  Discord webhook for the handoff notification.
                       Falls back to bh's ConsoleNotifier when unset.

Run:
  uv run examples/browser_use_shopping_handoff/local.py
"""

from __future__ import annotations

import asyncio
import logging
import os

# NOTE: browser-use's public API moves fast. If something breaks here,
# check the import line and `Agent(..., browser_session=...)` — older
# builds used `browser=...`.
from browser_use import ActionResult, Agent, BrowserSession, ChatAnthropic, Tools
from playwright.async_api import Browser, Page, async_playwright

from browser_handoff import Handoff, ServerConfig
from browser_handoff.detection import Detection
from browser_handoff.notifiers import DiscordNotifier, Notifier

# Quiet bh's INFO chatter so the demo output stays readable.
logging.basicConfig(level=logging.WARNING)

CDP_PORT = 9222
STREAMING_PORT = 8080

TASK = """\
Buy any t-shirt on https://automationexercise.com and reach the order
confirmation. Let human handle any step that requires intervention — login, signup, card entry, etc.\
"""


def _resolve_current_page(browser: Browser) -> Page | None:
    """Return the Playwright Page the agent is acting on.

    browser-use speaks CDP and doesn't hand us a Page; walk Playwright's
    view of the shared Chrome and pick the most recent non-blank tab.
    This flow uses a single tab, so that's reliably the right one.
    """
    pages: list[Page] = [
        p for ctx in browser.contexts for p in ctx.pages if not p.is_closed()
    ]
    non_blank = [p for p in pages if p.url and p.url != "about:blank"]
    return (non_blank or pages or [None])[0]


def _build_tools(
    h: Handoff,
    browser: Browser,
    agent_ref: dict[str, "Agent | None"],
) -> Tools:
    """Register `request_human_help` against the shared handoff + browser.

    `agent_ref` is a mutable holder populated after Agent(...) returns —
    Tools are built before the Agent exists. The tool uses it to
    pause/resume the agent around the handoff so browser-use's
    step_timeout doesn't race the human, and the agent doesn't race
    operator input inside the same page.
    """
    tools = Tools()

    @tools.action(
        "Hand off control to a human when you cannot proceed on your own — "
        "login walls, signup forms, identity verification, card / payment "
        "entry, anything that requires private credentials.\n"
        "\n"
        "Arguments:\n"
        "  reason: a short human-facing message shown in the stream viewer "
        "explaining what the human needs to do.\n"
        "  done_when: a natural-language description of ONE observable signal "
        "that proves the human did the action you needed. A vision model "
        "polls the page against this signal and the tool returns the moment "
        "it sees it.\n"
        "\n"
        "STRICT RULES for `done_when` — violating these makes the tool time out:\n"
        "\n"
        "  1. ONE signal, never multiple. NO `and`, `&`, commas joining "
        "conditions, `then`, or `followed by`. NOT 'X and Y'. NOT 'X "
        "showing Y'. NOT 'X is visible and Y is gone'. A single thing the "
        "model can confirm from a single screenshot.\n"
        "\n"
        "  2. DO NOT assume what page the human will leave you on. You will "
        "see whatever page they land on after the tool returns — describe "
        "the page state then, not now. The `done_when` should only capture "
        "the moment the human's work is over, NOT the next step in your "
        "plan.\n"
        "\n"
        "  3. Describe what is VISIBLE on the page, not what has happened "
        "behind the scenes. 'Logout link is visible' is observable. 'User "
        "is logged in' depends on the model inferring a hidden state and "
        "is less reliable.\n"
        "\n"
        "Good examples:\n"
        "  * 'a Logout link or username is visible somewhere on the page'\n"
        "  * 'an Order Placed or Thank You confirmation message is visible'\n"
        "  * 'the card-entry form is no longer visible'\n"
        "\n"
        "Bad examples (will time out):\n"
        "  * 'logged in AND back at checkout AND modal dismissed'  → compound\n"
        "  * 'logged in and the next step is visible'              → assumes destination\n"
        "  * 'the user has finished signup and is on the cart page' → compound + assumes\n"
    )
    async def request_human_help(reason: str, done_when: str) -> ActionResult:
        page = _resolve_current_page(browser)
        if page is None:
            return ActionResult(
                extracted_content="No active browser page available to hand off."
            )

        print(f"\n-> Handoff requested: {reason}\n   done_when: {done_when}\n")
        # Pause the agent while the human works — else browser-use's
        # step_timeout races the human's completion_timeout and the
        # shorter one wins, cancelling the tool call mid-handoff.
        # try/finally guarantees resume on timeout or error.
        agent = agent_ref["agent"]
        if agent is not None:
            agent.pause()
        try:
            result = await h.pause(
                page,
                until=Detection.llm(condition=done_when),
                reason=reason,
                name="shopping-handoff",
            )
        finally:
            if agent is not None:
                agent.resume()
        if result.timed_out:
            return ActionResult(
                extracted_content=(
                    "Human did not finish the step in time. Try a different "
                    "approach, or call request_human_help again with a clearer "
                    "`done_when` description."
                )
            )
        return ActionResult(
            extracted_content=(
                f"Human completed the step in {result.duration:.1f}s. "
                f"Current URL: {page.url}. You may continue the task."
            )
        )

    return tools


async def main() -> None:
    # Empty list → bh falls back to its built-in ConsoleNotifier.
    notifiers: list[Notifier] = []
    if webhook := os.getenv("DISCORD_WEBHOOK_URL"):
        notifiers.append(
            DiscordNotifier(webhook_url=webhook, username="Shopping Agent")
        )

    h = Handoff(
        server=ServerConfig(host="0.0.0.0", port=STREAMING_PORT),
        notifiers=notifiers,
    )

    async with async_playwright() as pw:
        # Both frameworks share one Chrome over CDP. launch() alone
        # gives a Browser whose `.contexts` only sees Playwright-owned
        # contexts; pages browser-use opens over its own CDP connection
        # are invisible. connect_over_cdp returns a handle that
        # enumerates every target — what _resolve_current_page needs.
        # window-size 1600x900 reads more naturally for a shopping site
        # than bh's 1280x800 default.
        launched = await pw.chromium.launch(
            headless=True,
            args=[
                f"--remote-debugging-port={CDP_PORT}",
                "--window-size=1600,900",
            ],
        )
        try:
            browser = await pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{CDP_PORT}"
            )
            # Populated after Agent(...) so the tool closure can pause/
            # resume the agent without a circular dependency.
            agent_ref: dict[str, Agent | None] = {"agent": None}
            tools = _build_tools(h, browser, agent_ref)
            browser_session = BrowserSession(cdp_url=f"http://127.0.0.1:{CDP_PORT}")
            agent = Agent(
                task=TASK,
                llm=ChatAnthropic(model="claude-sonnet-4-5"),
                browser_session=browser_session,
                tools=tools,
            )
            agent_ref["agent"] = agent
            await agent.run(max_steps=40)
        finally:
            await launched.close()


if __name__ == "__main__":
    asyncio.run(main())
