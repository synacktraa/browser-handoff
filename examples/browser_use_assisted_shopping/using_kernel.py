# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "browser-use",
#   "browser-handoff[llm] @ git+https://github.com/synacktraa/browser-handoff.git",
#   "kernel",
# ]
# ///
"""
Example: the same browser-use shopping agent, run on a Kernel cloud browser
in passthrough mode.

This is the Kernel-substrate variant of `local.py`. The agent loop, the
`request_human_help` tool, the detection contract, and the notifier wiring are
identical — the only swap is what's underneath: instead of launching a local
visible Chromium with --remote-debugging-port, we spin up a Kernel browser via
their SDK and connect to its `cdp_ws_url`.

Passthrough mode is the key shape here. browser-handoff's own CDP screencast
would have to pull every frame from Kernel's cloud Chrome back through this
local process and re-serve it to the operator — an unworkable round-trip over
WAN. Instead we pass Kernel's own viewer URL (`browser_live_view_url`) to
`wait_for_completion(stream_url=...)`, and browser-handoff:

  * skips its own screencast pump entirely
  * serves a wrapper page that iframes Kernel's viewer (cropped to just the
    page content, with browser-handoff's chrome around it)
  * runs the LLMDetection loop locally against the Page over CDP
  * pushes completion / expiration events to the wrapper over a status WS
  * installs a stealth in-page activity watcher so LLMDetection's gating
    still sees operator interaction (without bh ever seeing the operator's
    raw input — substrate delivers it directly to the page)

Architecture — one cloud Chrome shared over CDP:
  * `kernel.browsers.create()` returns a session with `cdp_ws_url` and
    `browser_live_view_url`.
  * Playwright connects to the cdp_ws_url — gives us the `Browser` whose
    `.contexts[*].pages[*]` the `request_human_help` tool walks to resolve
    the current Page.
  * browser-use connects to the SAME cdp_ws_url and drives the agent loop.
  * `request_human_help` calls `wait_for_completion(..., stream_url=...)`
    with Kernel's live-view URL. The operator opens browser-handoff's
    wrapper URL (printed in console / sent via Discord) and interacts with
    the iframed substrate view directly over WebRTC.

Prereqs:
  * KERNEL_API_KEY in the environment — used by the Kernel SDK.
  * ANTHROPIC_API_KEY in the environment — used both by browser-use's planner
    (ChatAnthropic) and by browser-handoff's LLMDetection.

Environment Variables (optional):
  DISCORD_WEBHOOK_URL  Discord webhook for the handoff ping. If unset,
                       browser-handoff prints a rich console panel instead.

Run:
  uv run examples/browser_use_assisted_shopping/using_kernel.py
"""

from __future__ import annotations

import asyncio
import logging
import os

# NOTE: browser-use's public API moves fast. If an import or method below
# fails, these are the lines most likely to need a version tweak:
#   1. `from browser_use import Agent, ActionResult, BrowserSession, ChatAnthropic, Tools`
#   2. `await browser_session.get_current_page_url()`
#   3. `Agent(..., browser_session=...)`  (older builds use `browser=...`)
from browser_use import ActionResult, Agent, BrowserSession, ChatAnthropic, Tools
from kernel import AsyncKernel
from playwright.async_api import Browser, Page, async_playwright

from browser_handoff import Handoff, ServerConfig
from browser_handoff.detection import Detection
from browser_handoff.notifiers import DiscordNotifier, Notifier

# Quiet the frame/mouse/screencast chatter so the demo output stays readable.
logging.basicConfig(level=logging.WARNING)

STREAMING_PORT = 8080

TASK = """\
Buy any t-shirt on https://automationexercise.com and reach the order
confirmation. Let human handle any step that requires intervention — login, signup, card entry, etc.\
"""


def _resolve_current_page(browser: Browser) -> Page | None:
    """Pick the Playwright page the agent is currently acting on.

    browser-use doesn't hand us a Page (it speaks CDP), so we walk Playwright's
    own view of the shared Chrome and return the most recent non-blank tab.
    For this flow the agent operates in a single tab, so that's reliably the
    one the agent just acted on.
    """
    pages: list[Page] = [
        p for ctx in browser.contexts for p in ctx.pages if not p.is_closed()
    ]
    non_blank = [p for p in pages if p.url and p.url != "about:blank"]
    return (non_blank or pages or [None])[0]


def _build_tools(
    handoff: Handoff,
    browser: Browser,
    stream_url: str,
    agent_ref: dict[str, "Agent | None"],
) -> Tools:
    """Register `request_human_help` against the shared handoff + browser.

    `stream_url` is Kernel's live-view URL, captured at browser creation
    time and reused for every handoff this session triggers — the substrate
    serves a single live-view URL for the whole browser session, not
    per-page or per-handoff.

    `agent_ref` is a mutable holder for the browser-use Agent. Tools are
    built before the Agent exists (the Agent constructor wants `tools=`),
    so we accept a dict the caller populates after Agent(...) returns.
    Used to pause/resume the agent loop around the handoff wait — keeps
    browser-use's step_timeout from racing the human.
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
        # Pause the agent loop while the human works. Otherwise browser-use's
        # step_timeout countdown races the human's session_timeout — whoever
        # is shorter wins, and the agent will cancel its own tool call mid-
        # handoff if step_timeout fires first. try/finally so a timeout or
        # error in the handoff still resumes the agent.
        agent = agent_ref["agent"]
        if agent is not None:
            agent.pause()
        try:
            result = await handoff.wait_for_completion(
                page,
                on=Detection.llm(condition=done_when),
                reason=reason,
                name="shopping-handoff",
                # Passthrough: browser-handoff iframes Kernel's live-view URL
                # in its wrapper page (cropped to page content) instead of
                # streaming frames itself. Operator interacts with the
                # substrate viewer directly over WebRTC.
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
    # Discord if configured; otherwise browser-handoff falls back to its
    # built-in ConsoleNotifier (rich panel with the stream URL).
    notifiers: list[Notifier] = []
    if webhook := os.getenv("DISCORD_WEBHOOK_URL"):
        notifiers.append(
            DiscordNotifier(webhook_url=webhook, username="Shopping Agent")
        )

    handoff = Handoff(
        server=ServerConfig(host="0.0.0.0", port=STREAMING_PORT),
        notifiers=notifiers,
    )

    # Kernel SDK reads KERNEL_API_KEY from the environment by default.
    kernel = AsyncKernel()
    kernel_browser = await kernel.browsers.create()
    cdp_url = kernel_browser.cdp_ws_url
    # Kernel's WebRTC live-view URL — what the operator's iframe will load
    # inside browser-handoff's wrapper page. Kept constant for the whole
    # browser session.
    live_view_url = kernel_browser.browser_live_view_url

    async with async_playwright() as pw:
        try:
            # Playwright handle on the cloud Chrome — `_resolve_current_page`
            # walks `.contexts[*].pages[*]` against this exact handle, so
            # both frameworks must connect to the *same* cdp_ws_url. The
            # cloud browser launches with a default context and page already
            # present, so don't create a new context — work with what's there.
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            # Holder for the agent — populated after Agent(...) returns so the
            # tool closure can reach it without a real circular dependency.
            agent_ref: dict[str, Agent | None] = {"agent": None}
            tools = _build_tools(handoff, browser, live_view_url, agent_ref)
            browser_session = BrowserSession(cdp_url=cdp_url)
            agent = Agent(
                task=TASK,
                llm=ChatAnthropic(model="claude-sonnet-4-5"),
                browser_session=browser_session,
                tools=tools,
            )
            agent_ref["agent"] = agent
            await agent.run(max_steps=40)
        finally:
            # Explicit delete; otherwise the cloud session lingers until the
            # configured idle timeout fires.
            await kernel.browsers.delete_by_id(kernel_browser.session_id)


if __name__ == "__main__":
    asyncio.run(main())
