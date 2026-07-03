# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "browser-use",
#   "browser-handoff[llm] @ git+https://github.com/synacktraa/browser-handoff.git",
#   "kernel",
# ]
# ///
"""
Example: the same browser-use shopping agent on a Kernel cloud browser
in passthrough mode.

Kernel-substrate variant of `local.py`. Agent loop, the
`request_human_help` tool, detection contract, and notifier wiring are
identical — only the underlying Chrome changes: instead of launching
locally with --remote-debugging-port, we create a Kernel browser and
connect to its `cdp_ws_url`.

Why passthrough: bh's own CDP screencast would pull every frame from
Kernel's cloud Chrome through this local process and re-serve it to
the operator — an unworkable WAN round-trip. Passing Kernel's
`browser_live_view_url` to `pause(stream_url=...)`
makes bh iframe that viewer in its wrapper instead, while keeping
detection + notification + lifecycle local.

Architecture — one cloud Chrome shared over CDP:
  * `kernel.browsers.create()` returns `cdp_ws_url` + `browser_live_view_url`.
  * Playwright connects to `cdp_ws_url`; the tool walks `.contexts[*]`
    on that handle to resolve the current Page.
  * browser-use connects to the SAME `cdp_ws_url` for its agent loop.
  * `request_human_help` calls `pause(stream_url=...)`
    with Kernel's live-view URL; the operator opens bh's wrapper URL
    (printed / Discord) and drives the iframed viewer over WebRTC.

Prereqs:
  * KERNEL_API_KEY in the environment.
  * ANTHROPIC_API_KEY — used by both browser-use's planner and bh's
    LLMDetection.

Environment Variables (optional):
  DISCORD_WEBHOOK_URL  Discord webhook; falls back to bh's
                       ConsoleNotifier when unset.

Run:
  uv run examples/browser_use_assisted_shopping/using_kernel.py
"""

from __future__ import annotations

import asyncio
import logging
import os

# NOTE: browser-use's public API moves fast. If something breaks here,
# check the import line and `Agent(..., browser_session=...)` — older
# builds used `browser=...`.
from browser_use import ActionResult, Agent, BrowserSession, ChatAnthropic, Tools
from kernel import AsyncKernel
from playwright.async_api import Browser, Page, async_playwright

from browser_handoff import Detection, Handoff, ServerConfig
from browser_handoff.notifiers import DiscordNotifier, Notifier

# Quiet bh's INFO chatter so the demo output stays readable.
logging.basicConfig(level=logging.WARNING)

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
    stream_url: str,
    agent_ref: dict[str, "Agent | None"],
) -> Tools:
    """Register `request_human_help` against the shared handoff + browser.

    `stream_url` is Kernel's live-view URL — one per session, not per
    page or per handoff. Reused for every handoff this run triggers.

    `agent_ref` is a mutable holder populated after Agent(...) returns
    so the tool can pause/resume the agent around the handoff —
    otherwise browser-use's step_timeout races the human.
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
                until=Detection.llm(
                    model="anthropic/claude-sonnet-4-6",
                    condition=done_when,
                ),
                reason=reason,
                name="shopping-handoff",
                # Passthrough — bh iframes Kernel's live-view URL
                # instead of streaming frames itself; the operator
                # drives it directly over WebRTC.
                stream_url=stream_url,
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

    # Kernel SDK reads KERNEL_API_KEY from env.
    kernel = AsyncKernel()
    kernel_browser = await kernel.browsers.create()
    cdp_url = kernel_browser.cdp_ws_url
    # Kernel's WebRTC live-view URL — iframed in bh's wrapper page.
    # One URL for the whole session.
    live_view_url = kernel_browser.browser_live_view_url

    async with async_playwright() as pw:
        try:
            # Both frameworks must share this exact handle so
            # _resolve_current_page sees the same `.contexts[*].pages[*]`
            # the agent is acting on. The cloud browser ships with a
            # default context + page; don't make a new one.
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            # Populated after Agent(...) so the tool closure can pause/
            # resume the agent without a circular dependency.
            agent_ref: dict[str, Agent | None] = {"agent": None}
            tools = _build_tools(h, browser, live_view_url, agent_ref)
            browser_session = BrowserSession(cdp_url=cdp_url)
            agent = Agent(
                task=TASK,
                llm=ChatAnthropic(model="claude-sonnet-4-6"),
                browser_session=browser_session,
                tools=tools,
            )
            agent_ref["agent"] = agent
            await agent.run(max_steps=40)
        finally:
            # Explicit delete — otherwise the cloud session lingers
            # until the configured idle timeout fires.
            await kernel.browsers.delete_by_id(kernel_browser.session_id)


if __name__ == "__main__":
    asyncio.run(main())
