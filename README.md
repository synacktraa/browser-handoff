# browser-handoff

Pause your browser automation, hand the page to a human, resume when they're done.

When automation hits something only a human should do — login, 2FA, OAuth consent, payment, identity check — `browser-handoff` streams the live browser to an operator over the web, waits for them to finish, then gives control back to your script.

## Install

```bash
pip install browser-handoff
```

LLM-based detection (optional): `pip install browser-handoff[llm]`

## 30-second example

```python
from playwright.async_api import async_playwright
from browser_handoff import Handoff, Scenario
from browser_handoff.detection import Detection

handoff = Handoff(
    scenarios=[
        Scenario(
            name="login",
            trigger=Detection.url(path_contains=["/login"]),
            complete=Detection.url(path_contains=["/dashboard"]),
        ),
    ],
)

async with async_playwright() as pw:
    browser = await pw.chromium.launch(headless=False)
    page = await browser.new_page()
    await page.goto("https://example.com/start")

    result = await handoff.run(page, timeout=30)
    if result.was_blocked and not result.timed_out:
        print(f"Human completed: {result.scenario_name}")

    # Continue automation
    await page.click("#continue")
```

## How it works

A `Scenario` is a pair: a `trigger` that says "stop, a human is needed" and a `complete` that says "OK, they're done."

`handoff.run(page, timeout=...)` watches the page for any scenario's trigger. If none fires within `timeout` seconds, it returns `HandoffResult(was_blocked=False)` and your script keeps going. If one fires, it starts a local streaming server, surfaces the URL (printed to logs and pushed to your notifiers), and waits until the matching `complete` condition matches — or until `server.completion_timeout` elapses, in which case the result has `timed_out=True`. `handoff.run` never raises on completion timeout; check the result.

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
    scenarios=[...],
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
    scenarios=[...],
    server=ServerConfig(
        host="127.0.0.1",                             # "0.0.0.0" to expose on LAN
        port=8080,
        public_base="https://my-tunnel.example.com",  # what notifiers link to
        completion_timeout=600,                       # max human wait (s)
        jpeg_quality=75,
        every_nth_frame=1,
    ),
)
```

## Config files

JSON or YAML, with `${VAR}` interpolation:

<table>
<tr><th>JSON</th><th>YAML</th></tr>
<tr>
<td>

```json
{
  "scenarios": [{
    "name": "login",
    "trigger": {
      "type": "any",
      "conditions": [
        { "type": "url",
          "path_contains": ["/login"] },
        { "type": "element",
          "present": ["input[type=password]"] }
      ]
    },
    "complete": {
      "type": "not",
      "condition": {
        "type": "url",
        "path_contains": ["/login"]
      }
    }
  }],
  "server": {
    "port": 8080,
    "public_base": "${HANDOFF_URL}"
  },
  "notifiers": [
    { "type": "slack",
      "webhook_url": "${SLACK_WEBHOOK}" }
  ]
}
```

</td>
<td>

```yaml
scenarios:
  - name: login
    trigger:
      type: any
      conditions:
        - type: url
          path_contains: ["/login"]
        - type: element
          present: ["input[type=password]"]
    complete:
      type: not
      condition:
        type: url
        path_contains: ["/login"]

server:
  port: 8080
  public_base: ${HANDOFF_URL}

notifiers:
  - type: slack
    webhook_url: ${SLACK_WEBHOOK}
```

</td>
</tr>
</table>

```python
handoff = Handoff.from_file("handoff.yaml")
# or: Handoff.from_json(s) / Handoff.from_yaml(s) / Handoff.from_dict(d)
```

## Examples

See [`examples/claude_oauth_login_handoff/`](examples/claude_oauth_login_handoff/) for a working Claude OAuth flow that pairs `browser-handoff` with [`ccauth`](https://github.com/synacktraa/ccauth) — `local.py` runs the flow on your machine; `in_daytona.py` runs the exact same `local.py` inside a Daytona sandbox so the human can log in from anywhere via the sandbox's preview URL.

## License

MIT — see [LICENSE](LICENSE).
