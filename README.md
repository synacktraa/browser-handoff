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
from browser_handoff import Detection, Handoff, Scenario

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
    if result.was_blocked:
        print(f"Human completed: {result.scenario_name}")

    # Continue automation
    await page.click("#continue")
```

## How it works

A `Scenario` is a pair: a `trigger` that says "stop, a human is needed" and a `complete` that says "OK, they're done."

`handoff.run(page, timeout=...)` watches the page for any scenario's trigger. If none fires within `timeout` seconds, it returns immediately and your script keeps going. If one does fire, it starts a local streaming server, surfaces the URL (printed to logs and pushed to your notifiers), and waits until the matching `complete` condition matches before returning.

## Detection

`Detection` is the factory for conditions:

```python
Detection.url(host_equals=["accounts.google.com"], path_contains=["/oauth"])
Detection.element(present=["#captcha"], visible=[".modal"], missing=[".loaded"])
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

Optional. Each notifier gets the stream URL when a handoff starts.

```python
from browser_handoff import DiscordNotifier, EmailNotifier, SlackNotifier

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
    ],
)
```

## Server

Defaults to `0.0.0.0:8080` with a 10-minute human-completion budget.

```python
from browser_handoff import ServerConfig

Handoff(
    scenarios=[...],
    server=ServerConfig(
        port=8080,
        public_base="https://my-tunnel.example.com",  # what notifiers link to
        timeout=600,                                  # max human wait (s)
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

See [`examples/`](examples/) for a working Claude OAuth flow that pairs `browser-handoff` with [`ccauth`](https://github.com/synacktraa/ccauth).

## License

MIT — see [LICENSE](LICENSE).
