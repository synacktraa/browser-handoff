"""browser-handoff - Human-in-the-loop fallback for browser automation.

A standalone library that provides human-in-the-loop fallback for browser
automation via CDP-based streaming when automation gets blocked.

Example:
    from browser_handoff import Handoff, Detection, ServerConfig

    handoff = Handoff(
        trigger_on=[
            Detection.content(title_contains=["Just a moment"]),
            Detection.element(present=[".captcha-container"]),
        ],
        complete_on=[
            Detection.url(
                host_equals=["localhost"],
                path_matches=["/callback"],
                query_contains=["code="],
            ),
        ],
        server=ServerConfig(port=8080),
    )

    async with handoff.guard(page) as session:
        await page.click("#login")
        # Auto-detects blockers and streams if needed
"""

from .detection import (
    AllDetection,
    AnyDetection,
    BaseDetection,
    ContentDetection,
    Detection,
    DetectionResult,
    ElementDetection,
    NotDetection,
    UrlDetection,
)
from .handoff import CompletionResult, GuardedSession, Handoff, HandoffError
from .notifiers import EmailNotifier, Notifier, SlackNotifier
from .server import ServerConfig, StreamingServer

__version__ = "0.1.0"

__all__ = [
    # Main classes
    "Handoff",
    "HandoffError",
    "CompletionResult",
    "GuardedSession",
    # Detection
    "Detection",
    "DetectionResult",
    "BaseDetection",
    "ContentDetection",
    "UrlDetection",
    "ElementDetection",
    "AllDetection",
    "AnyDetection",
    "NotDetection",
    # Server
    "ServerConfig",
    "StreamingServer",
    # Notifiers
    "Notifier",
    "SlackNotifier",
    "EmailNotifier",
]
