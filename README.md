# browser-handoff

Pause your browser automation, hand the page to a human, resume when they're done.

When automation hits something only a human should do — login, 2FA, OAuth consent, payment, identity check — `browser-handoff` streams the live browser to an operator over the web, waits for them to finish, then gives control back to your script.

## Install

```bash
pip install browser-handoff
```

LLM-based detection (optional): `pip install browser-handoff[llm]`

## 30-second example

Opens [the-internet.herokuapp.com/login](https://the-internet.herokuapp.com/login) — a public testing site that displays its own credentials on the page (`tomsmith` / `SuperSecretPassword!`). The handoff fires as soon as the page loads, prints a stream URL for you to open, and resumes once you sign in successfully.

[![Demo — login handoff](./.github/assets/heroku-app-login-handoff-thumbnail.png)](https://github.com/user-attachments/assets/493b2710-6b32-4593-b152-5f655b0c945e)

```python
import asyncio

from playwright.async_api import async_playwright

from browser_handoff import Handoff, Scenario
from browser_handoff.detection import Detection


async def main() -> None:
    handoff = Handoff()  # reusable: holds server + notifier config, nothing page-specific

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://the-internet.herokuapp.com/login")

        # Watch the page; hand off to a human when a trigger fires.
        result = await handoff.run(
            page,
            scenarios=[
                Scenario(
                    name="Heroku App Login",
                    trigger=Detection.url(path_contains=["/login"]),
                    complete=Detection.url(path_contains=["/secure"]),
                ),
            ],
            timeout=10,
        )
        if result.was_blocked and not result.timed_out:
            print(f"Human completed: {result.scenario_name} in {result.duration:.1f}s")

        # Back in script mode — confirm we landed on the post-login page.
        print(f"Now at: {page.url}")
        await browser.close()


asyncio.run(main())
```

## How it works

A `Handoff` holds your transport config — the streaming server and notifiers — and is reusable across pages and runs. You decide *what* to watch for per call, so the same `Handoff` serves any number of scenarios.

**Let the library detect the moment** with `handoff.run(page, scenarios=[...])`. A `Scenario` is a pair: a `trigger` that says "stop, a human is needed" and a `complete` that says "OK, they're done." `run` watches every scenario's trigger. If none fires within `timeout` seconds, it returns `HandoffResult(was_blocked=False)` and your script keeps going. If one fires, it starts a local streaming server, surfaces the URL (printed to logs and pushed to your notifiers), and waits until that scenario's `complete` matches — or until `server.session_timeout` elapses, in which case the result has `timed_out=True`. It never raises on timeout; check the result.

**Already know a human is needed?** Skip trigger detection and stream right away with `handoff.wait_for_completion(page, on=...)`. This is the right call when something upstream already decided — e.g. an AI agent navigated to the payment page itself — so watching for a trigger would be redundant:

```python
await handoff.wait_for_completion(
    page,
    on=Detection.url(path_contains=["/payment_done"]),
    reason="Payment page reached",
)
```

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

Defaults to `127.0.0.1:8080` (loopback only) with a 10-minute human-completion budget. Set `host="0.0.0.0"` to expose on the LAN — e.g. for phone access or tunnel forwarding.

```python
from browser_handoff import ServerConfig

Handoff(
    server=ServerConfig(
        host="127.0.0.1",                             # "0.0.0.0" to expose on LAN
        port=8080,
        public_base="https://my-tunnel.example.com",  # what notifiers link to
        session_timeout=600,                          # max session lifetime / human wait (s)
        jpeg_quality=75,
        every_nth_frame=1,
    ),
)
```

### Access control

The stream URL carries a high-entropy capability token (`…/?t=<token>`): whoever holds the link can view **and control** the page, so treat it like a password. The token is unguessable, decoupled from internal ids, and expires when the handoff finishes or `session_timeout` elapses — a stale link stops working. When exposing beyond loopback (`0.0.0.0`, a tunnel, or a sandbox preview URL), **serve over HTTPS/WSS** so the token isn't readable in transit; set `public_base` to your public `https://` origin and the operator link is built from it. There is no second factor yet — one leaked, still-active link grants control, so deliver it over a trusted channel.

## Examples

- [`Claude OAuth login handoff`](examples/claude_oauth_login_handoff/) — a working Claude OAuth flow that pairs `browser-handoff` with [`ccauth`](https://github.com/synacktraa/ccauth). `local.py` runs the flow on your machine; `in_daytona.py` runs the exact same `local.py` inside a Daytona sandbox so the human can log in from anywhere via the sandbox's preview URL.
- [`browser-use assisted shopping`](examples/browser_use_assisted_shopping/) — a [`browser-use`](https://github.com/browser-use/browser-use) agent buys a t-shirt on automationexercise.com. `browser-handoff` is exposed to the agent as a custom tool; the agent decides on its own when to call it (the login wall and the card form), and a human takes over for those steps while the agent drives the rest.

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
