"""browser-handoff - Human-in-the-loop fallback for browser automation.

A standalone library that provides human-in-the-loop fallback for browser
automation via CDP-based streaming when automation gets blocked.

Example:
    from browser_handoff import Handoff, Detection, Scenario

    handoff = Handoff(
        scenarios=[
            Scenario(
                name="login_required",
                trigger=Detection.element(selector='input[type="email"]'),
                complete=Detection.url(path_contains=["/dashboard"]),
            ),
        ],
    )

    result = await handoff.run(page)
    if result.was_blocked:
        print(f"Human completed: {result.scenario_name}")
    await bot_logic(page)
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
from .handoff import CompletionResult, Handoff, HandoffError, HandoffResult
from .scenario import Scenario
from .notifiers import DiscordNotifier, EmailNotifier, Notifier, SlackNotifier
from .server import ServerConfig, StreamingServer

__version__ = "0.1.0"

__all__ = [
    # Main classes
    "Handoff",
    "HandoffError",
    "HandoffResult",
    "CompletionResult",
    "Scenario",
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
    "DiscordNotifier",
    "EmailNotifier",
]
