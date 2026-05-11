# browser-handoff

A standalone library that provides human-in-the-loop fallback for browser automation via CDP-based streaming when automation gets blocked.

## Features

- **Detection System**: Flexible rules to detect when automation is blocked (CAPTCHAs, login pages, security challenges)
- **CDP Streaming**: Real-time browser streaming via Chrome DevTools Protocol
- **Event-Driven**: Efficient event-based detection instead of polling
- **Notifications**: Alert humans via Slack, Discord, email, or custom notifiers
- **Config Files**: JSON/YAML configuration with environment variable interpolation
- **LLM Detection**: Optional AI-powered detection using vision models

## Installation

```bash
pip install browser-handoff
```

For LLM-based detection:

```bash
pip install browser-handoff[llm]
```

## Quick Start

```python
from playwright.async_api import async_playwright
from browser_handoff import Handoff, Detection, ServerConfig

# Create handoff configuration
handoff = Handoff(
    trigger_on=[
        Detection.content(
            title_contains=["Just a moment", "Access Denied"],
            body_contains=["challenges.cloudflare.com"],
        ),
        Detection.element(
            present=[".cf-turnstile", "#captcha"],
        ),
    ],
    complete_on=[
        Detection.url(
            host_equals=["localhost"],
            path_matches=["/callback"],
            query_contains=["code="],
        ),
    ],
    server=ServerConfig(
        port=8080,
        timeout=600,
    ),
)

async with async_playwright() as p:
    browser = await p.chromium.launch(headless=False)
    page = await browser.new_page()

    await page.goto("https://example.com/oauth/authorize")

    # Guard monitors for blockers and streams to human if needed
    async with handoff.guard(page) as session:
        await page.click("#login")
        # If a blocker is detected, streams to human
        # Waits for completion condition automatically

    if session.was_blocked:
        print(f"Human helped! Completed via: {session.completion_result.detection_type}")
```

## Detection Types

### Content Detection

Check page title or body for substrings or regex patterns:

```python
Detection.content(
    title_contains=["Just a moment", "Access Denied"],
    title_matches=[r"Challenge \d+"],
    body_contains=["captcha", "security check"],
    body_matches=[r"verify.*human"],
)
```

### URL Detection

Check URL components:

```python
Detection.url(
    scheme_equals="https",
    host_equals=["localhost", "accounts.google.com"],
    host_not_equals=["blocked-domain.com"],
    path_matches=["/callback", "/oauth/.*"],
    path_contains=["/auth/"],
    query_contains=["code=", "token="],
)
```

### Element Detection

Check for DOM element presence, absence, or visibility:

```python
Detection.element(
    present=[".captcha-container", "#challenge-form"],
    missing=["button#submit", ".main-content"],
    visible=[".modal-overlay"],
    hidden=[".loading-spinner"],
)
```

### LLM Detection (Optional)

Use AI vision to analyze screenshots:

```python
Detection.llm(
    model="anthropic/claude-sonnet-4-20250514",
    condition="The page is showing a CAPTCHA or security challenge",
)
```

Requires `pip install browser-handoff[llm]` and appropriate API keys.

### Combinators

Combine detections with logical operators:

```python
# AND - all conditions must match
Detection.all([
    Detection.url(path_matches=["/dashboard"]),
    Detection.element(present=[".user-avatar"]),
])

# OR - any condition must match
Detection.any([
    Detection.element(present=["#success"]),
    Detection.content(body_contains=["Welcome"]),
])

# NOT - invert a condition
Detection.not_(
    Detection.element(present=[".error-message"])
)
```

## Scenarios

Scenarios allow you to define trigger-completion pairs where each trigger has its own specific completion condition. Only one scenario can be active at a time - when a scenario's trigger is detected, its corresponding completion condition is used.

### Programmatic Usage

```python
from browser_handoff import Handoff, Detection, Scenario, ServerConfig

handoff = Handoff(
    scenarios=[
        Scenario(
            name="cloudflare_challenge",
            trigger=Detection.content(title_contains=["Just a moment"]),
            complete=Detection.element(missing=[".cf-turnstile"]),
        ),
        Scenario(
            name="login_required",
            trigger=Detection.url(path_contains=["/login"]),
            complete=Detection.url(path_contains=["/dashboard"]),
        ),
        Scenario(
            name="recaptcha",
            trigger=Detection.element(present=[".g-recaptcha"]),
            complete=Detection.element(missing=[".g-recaptcha"]),
        ),
    ],
    server=ServerConfig(port=8080),
)

async with handoff.guard(page) as session:
    await page.click("#login")
    # Automatically detects which scenario matched
    # Uses that scenario's completion condition

    if session.was_blocked:
        print(f"Scenario: {session.scenario_name}")
        print(f"Completed via: {session.completion_result.detection_type}")
```

### YAML Configuration

```yaml
scenarios:
  - name: cloudflare_challenge
    trigger:
      type: content
      title_contains:
        - "Just a moment"
    complete:
      type: element
      missing:
        - ".cf-turnstile"

  - name: google_oauth
    trigger:
      type: url
      host_equals:
        - accounts.google.com
    complete:
      type: url
      host_equals:
        - localhost
      query_contains:
        - "code="

  - name: recaptcha
    trigger:
      type: element
      present:
        - ".g-recaptcha"
    complete:
      type: element
      missing:
        - ".g-recaptcha"

server:
  port: 8080
  timeout: 600
```

### JSON Configuration

```json
{
  "scenarios": [
    {
      "name": "cloudflare_challenge",
      "trigger": {
        "type": "content",
        "title_contains": ["Just a moment"]
      },
      "complete": {
        "type": "element",
        "missing": [".cf-turnstile"]
      }
    },
    {
      "name": "hcaptcha",
      "trigger": {
        "type": "element",
        "present": [".h-captcha"]
      },
      "complete": {
        "type": "element",
        "missing": [".h-captcha"]
      }
    }
  ],
  "server": {
    "port": 8080
  }
}
```

### Mixing Scenarios with Global Triggers

You can use scenarios alongside global `trigger_on` and `complete_on`. Scenarios are checked first, and global triggers serve as a fallback:

```python
handoff = Handoff(
    scenarios=[
        Scenario(
            name="cloudflare",
            trigger=Detection.content(title_contains=["Just a moment"]),
            complete=Detection.element(missing=[".cf-turnstile"]),
        ),
    ],
    # Fallback for unknown blockers
    trigger_on=[
        Detection.element(present=[".unknown-captcha"]),
    ],
    complete_on=[
        Detection.url(query_contains=["success=true"]),
    ],
)
```

## Configuration Files

### JSON Configuration

```json
{
  "trigger_on": [
    {
      "type": "content",
      "title_contains": ["Just a moment"],
      "body_contains": ["challenges.cloudflare.com"]
    },
    {
      "type": "element",
      "present": [".cf-turnstile", "#captcha"]
    }
  ],
  "complete_on": [
    {
      "type": "url",
      "host_equals": ["localhost"],
      "path_matches": ["/callback"],
      "query_contains": ["code="]
    }
  ],
  "server": {
    "host": "0.0.0.0",
    "port": 8080,
    "public_base": "${HANDOFF_PUBLIC_URL}",
    "timeout": 600
  },
  "notifiers": [
    {
      "type": "slack",
      "webhook_url": "${SLACK_WEBHOOK_URL}"
    }
  ]
}
```

### YAML Configuration

```yaml
trigger_on:
  - type: content
    title_contains:
      - "Just a moment"
      - "Access Denied"
    body_contains:
      - "challenges.cloudflare.com"

  - type: element
    present:
      - ".cf-turnstile"
      - "#captcha"

complete_on:
  - type: url
    host_equals:
      - localhost
    path_matches:
      - "/callback"
    query_contains:
      - "code="

server:
  port: 8080
  public_base: ${HANDOFF_PUBLIC_URL}
  timeout: 600

notifiers:
  - type: slack
    webhook_url: ${SLACK_WEBHOOK_URL}
```

### Loading Configuration

```python
from browser_handoff import Handoff

# From file
handoff = Handoff.from_file("config.json")
handoff = Handoff.from_file("config.yaml")

# From string
handoff = Handoff.from_json(json_string)
handoff = Handoff.from_yaml(yaml_string)
```

## Notifiers

### Slack

```python
from browser_handoff import SlackNotifier

notifier = SlackNotifier(
    webhook_url="https://hooks.slack.com/services/...",
    channel="#alerts",  # Optional
    username="Browser Bot",  # Optional
)
```

### Discord

```python
from browser_handoff import DiscordNotifier

notifier = DiscordNotifier(
    webhook_url="https://discord.com/api/webhooks/...",
    username="Browser Bot",  # Optional
    avatar_url="https://example.com/avatar.png",  # Optional
)
```

### Email

```python
from browser_handoff import EmailNotifier

notifier = EmailNotifier(
    smtp_host="smtp.gmail.com",
    smtp_port=587,
    username="bot@example.com",
    password="app-password",
    to=["ops@example.com", "admin@example.com"],
    use_tls=True,
)
```

## Server Configuration

```python
from browser_handoff import ServerConfig

config = ServerConfig(
    host="0.0.0.0",      # Bind address
    port=8080,           # Port number
    public_base="https://proxy.example.com",  # Public URL for notifications
    timeout=600,         # Timeout in seconds
)
```

## Manual Control

For more control over the handoff process:

```python
# Check if currently blocked
is_blocked, result = await handoff.is_blocked(page)
if is_blocked:
    print(f"Blocked: {result.reason}")

# Check if task is complete
is_complete, result = await handoff.is_complete(page)

# Manually trigger handoff
completion = await handoff.wait_for_human(
    page=page,
    context=context,
    reason="Manual intervention requested",
)
print(f"Completed via: {completion.detection_type}")
```

## Environment Variables

Configuration files support `${VAR_NAME}` syntax for environment variable interpolation:

```json
{
  "server": {
    "public_base": "${HANDOFF_URL}"
  },
  "notifiers": [
    {
      "type": "slack",
      "webhook_url": "${SLACK_WEBHOOK}"
    }
  ]
}
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run specific tests
pytest tests/test_detection.py -v
```

## License

MIT License - see [LICENSE](LICENSE) for details.
