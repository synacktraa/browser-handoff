# browser-handoff

Pause your browser automation, hand the page to a human, resume when they're done.

When your script or agent hits something only a human should do — login, 2FA, OAuth consent, payment, identity check — `browser-handoff` streams the live browser to an operator over the web, waits for them to finish, then gives control back to your code.

[![Demo — browser-use shopping agent × Kernel](./.github/assets/browser-use-shopping-handoff-onkernel-thumbnail.png)](https://github.com/user-attachments/assets/1e4a5e17-d792-45b1-852e-fd2b4ce15715)

> A [`browser-use`](https://github.com/browser-use/browser-use) shopping agent × [Kernel](https://onkernel.com) cloud browser — the agent decides when it needs human assistance. Refer: [`examples/browser_use_assisted_shopping/using_kernel.py`](examples/browser_use_assisted_shopping/using_kernel.py).

## Install

```bash
pip install browser-handoff
```

LLM-based detection (optional): `pip install browser-handoff[llm]`

## 30-second example

Opens a login page, streams it to a human, and resumes when they sign in.

[![Demo — login handoff](./.github/assets/heroku-app-login-handoff-thumbnail.png)](https://github.com/user-attachments/assets/e2990cc6-44c1-48c4-b9df-85b2e091e09f)

```python
import asyncio

from playwright.async_api import async_playwright

from browser_handoff import Detection, Handoff


async def main() -> None:
    h = Handoff()  # reusable: holds server + notifier config, nothing page-specific

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://the-internet.herokuapp.com/login")

        # Stream the page and pause until the human lands on /secure.
        result = await h.pause(
            page,
            until=Detection.url(path_contains=["/secure"]),
            reason="Sign in with the credentials shown on the page",
        )
        if not result.timed_out:
            print(f"Human signed in in {result.duration:.1f}s")

        print(f"Now at: {page.url}")
        await browser.close()


asyncio.run(main())
```

## How it works

A `Handoff` is a reusable transport config — the streaming server, notifiers, viewport — shared across pages and runs. It exposes two methods for the two shapes of handoff:

**`h.pause(page, until=...)`** — you already know a human is needed. Common when an AI agent hits a wall and calls a `request_human_help` tool. Streams the page immediately, pauses until the detection matches.

**`h.guard(page, scenarios=[...])`** — you want the library to decide. Give it one or more `Scenario` pairs (start condition + resume condition). It watches; if a scenario fires, it hands off. If nothing fires within `trigger_timeout`, your script keeps going.

```python
from browser_handoff import Detection, Handoff, Scenario

h = Handoff()

# Explicit: your code decides. A fuzzy resume condition works well here —
# a vision model polls the page against `condition` and pause returns
# the moment it sees the described state.
await h.pause(
    page,
    until=Detection.llm(condition="user is signed in"),
    reason="Sign in to continue",
)

# Declarative: library watches for a login page and hands off if it appears.
await h.guard(
    page,
    scenarios=[
        Scenario(
            name="login",
            on=Detection.url(path_contains=["/login"]),
            until=Detection.url(path_contains=["/secure"]),
        ),
    ],
    trigger_timeout=10,
)
```

Both return a `HandoffResult`. On a clean finish: `was_blocked=True, timed_out=False`.

On timeout the result has `timed_out=True` and `timeout_cause` names the timer that fired — `"access"` (nobody opened the link) or `"completion"` (they opened it but didn't finish). Neither method ever raises on timeout; check the result.

## Passthrough mode (cloud substrates)

When the page lives in a cloud browser substrate (Kernel, Browserbase, Steel, Cua, …) and `browser-handoff` runs on your machine, screencast mode relays every frame and every operator input through your local process — each interaction round-trips over the WAN between your machine and the substrate. Frames tolerate it; input doesn't. Observed input latency against a Kernel cloud browser in screencast mode was ~30 seconds per keystroke — unusable for filling out a form.

Most substrates already ship their own first-class viewer. Passthrough mode delegates streaming and input to that viewer while `browser-handoff` keeps the detection, notification, and lifecycle responsibilities.

Same Heroku login, this time on a [Kernel](https://onkernel.com) cloud browser:

```python
import asyncio

from kernel import AsyncKernel
from playwright.async_api import async_playwright

from browser_handoff import Detection, Handoff


async def main() -> None:
    h = Handoff()
    kernel = AsyncKernel()
    kernel_browser = await kernel.browsers.create()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(kernel_browser.cdp_ws_url)
        # Kernel browsers boot with one context + one page already attached.
        page = (browser.contexts[0] or await browser.new_context()).pages[0]
        await page.goto("https://the-internet.herokuapp.com/login")

        result = await h.pause(
            page,
            until=Detection.url(path_contains=["/secure"]),
            reason="Sign in with the credentials shown on the page",
            stream_url=kernel_browser.browser_live_view_url,  # ← passthrough
        )
        if not result.timed_out:
            print(f"Human signed in in {result.duration:.1f}s")

    await kernel.browsers.delete_by_id(kernel_browser.session_id)


asyncio.run(main())
```

What changes when `stream_url` is set:

- No local CDP screencast pump — the substrate's viewer owns frames.
- The operator opens a `browser-handoff`-served wrapper URL that iframes the substrate's viewer, cropped to just the page content.
- Window maximization at handoff start gives the crop math a clean, deterministic rect.
- LLMDetection stays stealthy — its in-page observer arms only after the operator opens the wrapper, so the substrate's viewer URL alone can't drive vision calls.

`stream_url` works on both entry points — `h.pause(stream_url=...)` and `h.guard(stream_url=...)`. Everything else (detections, notifiers, timers) is identical.

## Scope: what this is *not*

`browser-handoff` is for flows gated by **credentials or session state** — login pages, 2FA prompts, OAuth consent screens, payment forms, identity verification, T&C acceptance.

It is **not** an anti-bot bypass. Sites that fingerprint Playwright/CDP sessions as automation will keep refusing the flow even after a human solves a CAPTCHA, Cloudflare Turnstile, or similar challenge — the session itself is flagged, not the response. If that's your problem, you need an anti-detection browser, not a handoff tool.

## Detection

`Detection` is the factory for conditions:

```python
Detection.url(host_equals=["accounts.google.com"], path_contains=["/oauth"])
Detection.element(present=["input[type=password]"], visible=[".consent-modal"], missing=[".user-menu"])
Detection.content(title_contains=["Sign In"], body_matches=[r"verify.*you"])
Detection.llm(model="anthropic/claude-sonnet-4-5", condition="Login form is visible")
```

Combine them:

```python
Detection.any([d1, d2])    # OR
Detection.all([d1, d2])    # AND
Detection.not_(d1)         # NOT
```

## Notifications

If you pass no notifiers, the library falls back to a built-in `ConsoleNotifier` that prints a rich panel to stdout with the stream URL — so the link is always somewhere obvious. When you do pass notifiers, the library stays out of the way and only fires what you configured.

```python
from browser_handoff.notifiers import (
    ConsoleNotifier, DiscordNotifier, EmailNotifier, SlackNotifier,
)

Handoff(
    notifiers=[
        SlackNotifier(webhook_url="https://hooks.slack.com/..."),
        DiscordNotifier(webhook_url="https://discord.com/api/webhooks/..."),
        EmailNotifier(
            smtp_host="smtp.gmail.com", smtp_port=587,
            username="bot@x.com", password="...",
            to=["ops@x.com"],
        ),
        ConsoleNotifier(),  # explicit — add alongside others if you also want a local panel
    ],
)
```

## Server

Defaults to `127.0.0.1:8080` (loopback only) with a 10-minute access budget and a 30-minute post-connect completion budget. Set `host="0.0.0.0"` to expose on the LAN — e.g. for phone access or tunnel forwarding.

```python
from browser_handoff import ServerConfig

Handoff(
    server=ServerConfig(
        host="127.0.0.1",                             # "0.0.0.0" to expose on LAN
        port=8080,
        public_base="https://my-tunnel.example.com",  # what notifiers link to
        access_timeout=600,                           # pre-connect bound (s)
        completion_timeout=1800,                      # post-connect work budget (s)
        jpeg_quality=75,
        every_nth_frame=1,
    ),
)
```

### Access control

**Token in the URL.** The stream URL carries a high-entropy capability token (`…/?t=<token>`). Whoever holds the link can view **and control** the page — treat it like a password.

**Token lifetime.** The token is unguessable, decoupled from internal ids, and expires when the handoff ends or the worst-case session lifetime (`access_timeout + completion_timeout`) elapses. Stale links stop working.

**Exposure beyond loopback.** Serving on `0.0.0.0`, a tunnel, or a sandbox preview URL? Use **HTTPS/WSS** so the token isn't readable in transit; set `public_base` to your public `https://` origin and notifier links are built from it.

**No second factor yet.** One leaked, still-active link grants control. Deliver it over a trusted channel.

## Examples

**[Claude OAuth login handoff](examples/claude_oauth_login_handoff/)** — a working Claude OAuth flow paired with [`ccauth`](https://github.com/synacktraa/ccauth).
- `local.py` — runs on your machine.
- `in_daytona.py` — runs the same `local.py` inside a Daytona sandbox, so the human can sign in from anywhere via the sandbox's preview URL.

**[Browser-Use assisted shopping](examples/browser_use_assisted_shopping/)** — a [`browser-use`](https://github.com/browser-use/browser-use) agent buys a t-shirt on automationexercise.com. `browser-handoff` is exposed to the agent as a custom tool; the agent decides on its own when to call it (login wall, card form), and a human takes over for those steps while the agent drives the rest.
- `local.py` — local Chromium.
- `using_kernel.py` — [Kernel](https://onkernel.com) cloud browser in passthrough mode; the operator's wrapper iframes Kernel's WebRTC live view directly, no double-hop streaming.

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
