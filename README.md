# browser-handoff

A standalone library that provides human-in-the-loop fallback for browser automation. Seamlessly hand off control to humans for tasks that require genuine human interaction - authentication, payments, identity verification, or any workflow where automation isn't appropriate.

## Why browser-handoff?

Some tasks simply require a human:
- **Authentication flows** - Login with 2FA, OAuth consent screens, SSO
- **Payment processing** - Entering payment details, confirming purchases
- **Identity verification** - Document uploads, biometric checks
- **Account setup** - Registration forms, consent agreements
- **Sensitive actions** - Approving transactions, confirming deletions

Browser-handoff detects when your automation reaches these points and streams the browser to a human operator who can complete the task, then seamlessly returns control to your automation.

## Features

- **Detection System**: Flexible rules to detect when human intervention is needed
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
from browser_handoff import Handoff, Detection, Scenario, ServerConfig

# Create handoff configuration with scenarios
# Each scenario pairs a trigger with its specific completion condition
# Use Detection.any() or Detection.all() to combine multiple conditions
handoff = Handoff(
    scenarios=[
        Scenario(
            name="login_required",
            # Trigger when login page is detected
            trigger=Detection.any([
                Detection.url(path_contains=["/login", "/signin"]),
                Detection.element(present=["form[action*=login]"]),
            ]),
            # Complete when redirected to dashboard
            complete=Detection.url(path_contains=["/dashboard", "/home"]),
        ),
        Scenario(
            name="oauth_consent",
            # Trigger on OAuth consent screens
            trigger=Detection.url(host_equals=["accounts.google.com"]),
            # Complete when redirected back with auth code
            complete=Detection.all([
                Detection.url(host_equals=["localhost"]),
                Detection.url(query_contains=["code="]),
            ]),
        ),
        Scenario(
            name="payment_flow",
            # Trigger on payment pages
            trigger=Detection.any([
                Detection.url(path_contains=["/checkout", "/payment"]),
                Detection.element(present=["#card-number", ".payment-form"]),
            ]),
            # Complete when payment confirmed
            complete=Detection.any([
                Detection.url(path_contains=["/confirmation", "/success"]),
                Detection.element(present=[".payment-success"]),
            ]),
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

    await page.goto("https://example.com/start")

    # Guard monitors and streams to human when needed
    async with handoff.guard(page) as session:
        await page.click("#begin-checkout")
        # If login/payment is needed, streams to human
        # Human completes the task, automation resumes

    if session.was_blocked:
        print(f"Scenario: {session.scenario_name}")
        print(f"Human helped! Completed via: {session.completion_result.detection_type}")
```

## Detection Types

### Content Detection

Check page title or body for substrings or regex patterns:

```python
Detection.content(
    title_contains=["Sign In", "Login"],
    title_matches=[r"Step \d+ of \d+"],
    body_contains=["enter your password", "verify your identity"],
    body_matches=[r"welcome.*back"],
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
    path_contains=["/auth/", "/login"],
    query_contains=["code=", "token="],
)
```

### Element Detection

Check for DOM element presence, absence, or visibility:

```python
Detection.element(
    present=["input[type=password]", "#login-form"],
    missing=[".user-menu", ".logout-button"],
    visible=[".modal-overlay"],
    hidden=[".loading-spinner"],
)
```

### LLM Detection (Optional)

Use AI vision to analyze screenshots:

```python
Detection.llm(
    model="anthropic/claude-sonnet-4-20250514",
    condition="The page is showing a login form or asking for user credentials",
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

Scenarios define trigger-completion pairs where each trigger has its own specific completion condition. Only one scenario can be active at a time - when a scenario's trigger is detected, its corresponding completion condition is used.

Use `Detection.any()` to trigger on multiple conditions (OR logic), and `Detection.all()` when all conditions must be met (AND logic).

### Programmatic Usage

```python
from browser_handoff import Handoff, Detection, Scenario, ServerConfig

handoff = Handoff(
    scenarios=[
        Scenario(
            name="login_flow",
            # Trigger on login pages
            trigger=Detection.any([
                Detection.url(path_contains=["/login", "/signin", "/auth"]),
                Detection.element(present=["input[type=password]"]),
            ]),
            # Complete when logged in
            complete=Detection.any([
                Detection.url(path_contains=["/dashboard", "/home", "/account"]),
                Detection.element(present=[".user-menu", ".logout-button"]),
            ]),
        ),
        Scenario(
            name="two_factor_auth",
            # Trigger on 2FA/MFA pages
            trigger=Detection.any([
                Detection.url(path_contains=["/2fa", "/mfa", "/verify"]),
                Detection.element(present=[".otp-input", "#authenticator-code"]),
            ]),
            # Complete when verification done
            complete=Detection.url(path_contains=["/dashboard", "/home"]),
        ),
        Scenario(
            name="payment_checkout",
            # Trigger on payment pages
            trigger=Detection.any([
                Detection.url(path_contains=["/checkout", "/payment", "/billing"]),
                Detection.element(present=[".stripe-card", "#card-element"]),
            ]),
            # Complete when order confirmed
            complete=Detection.all([
                Detection.url(path_contains=["/confirmation", "/thank-you"]),
                Detection.not_(Detection.element(present=[".payment-error"])),
            ]),
        ),
        Scenario(
            name="oauth_consent",
            # Trigger on OAuth provider consent screens
            trigger=Detection.url(host_equals=["accounts.google.com"]),
            # Complete when redirected back with auth code
            complete=Detection.all([
                Detection.url(host_equals=["localhost"]),
                Detection.url(query_contains=["code="]),
            ]),
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
  - name: login_flow
    # Use 'any' to trigger on multiple conditions (OR logic)
    trigger:
      type: any
      conditions:
        - type: url
          path_contains:
            - "/login"
            - "/signin"
        - type: element
          present:
            - "input[type=password]"
    complete:
      type: url
      path_contains:
        - "/dashboard"
        - "/home"

  - name: two_factor_auth
    trigger:
      type: any
      conditions:
        - type: url
          path_contains:
            - "/2fa"
            - "/mfa"
        - type: element
          present:
            - ".otp-input"
    complete:
      type: url
      path_contains:
        - "/dashboard"

  - name: oauth_consent
    trigger:
      type: url
      host_equals:
        - accounts.google.com
    # Use 'all' when all conditions must be met (AND logic)
    complete:
      type: all
      conditions:
        - type: url
          host_equals:
            - localhost
        - type: url
          query_contains:
            - "code="

  - name: payment_checkout
    trigger:
      type: any
      conditions:
        - type: url
          path_contains:
            - "/checkout"
            - "/payment"
        - type: element
          present:
            - "#card-element"
            - ".stripe-card"
    complete:
      type: url
      path_contains:
        - "/confirmation"
        - "/thank-you"

server:
  port: 8080
  timeout: 600
```

### JSON Configuration

```json
{
  "scenarios": [
    {
      "name": "login_flow",
      "trigger": {
        "type": "any",
        "conditions": [
          { "type": "url", "path_contains": ["/login", "/signin"] },
          { "type": "element", "present": ["input[type=password]"] }
        ]
      },
      "complete": {
        "type": "url",
        "path_contains": ["/dashboard", "/home"]
      }
    },
    {
      "name": "oauth_consent",
      "trigger": {
        "type": "url",
        "host_equals": ["accounts.google.com"]
      },
      "complete": {
        "type": "all",
        "conditions": [
          { "type": "url", "host_equals": ["localhost"] },
          { "type": "url", "query_contains": ["code="] }
        ]
      }
    }
  ],
  "server": {
    "port": 8080
  }
}
```

## Configuration Files

### JSON Configuration

```json
{
  "scenarios": [
    {
      "name": "login_flow",
      "trigger": {
        "type": "any",
        "conditions": [
          { "type": "url", "path_contains": ["/login", "/signin"] },
          { "type": "element", "present": ["input[type=password]"] }
        ]
      },
      "complete": {
        "type": "url",
        "path_contains": ["/dashboard", "/home"]
      }
    },
    {
      "name": "payment_checkout",
      "trigger": {
        "type": "url",
        "path_contains": ["/checkout", "/payment"]
      },
      "complete": {
        "type": "all",
        "conditions": [
          { "type": "url", "path_contains": ["/confirmation"] },
          { "type": "not", "condition": { "type": "element", "present": [".error"] } }
        ]
      }
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
scenarios:
  - name: login_flow
    trigger:
      type: any
      conditions:
        - type: url
          path_contains:
            - "/login"
            - "/signin"
        - type: element
          present:
            - "input[type=password]"
    complete:
      type: url
      path_contains:
        - "/dashboard"
        - "/home"

  - name: payment_checkout
    trigger:
      type: url
      path_contains:
        - "/checkout"
        - "/payment"
    complete:
      type: url
      path_contains:
        - "/confirmation"
        - "/thank-you"

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
