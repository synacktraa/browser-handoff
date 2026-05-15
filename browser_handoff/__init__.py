"""browser-handoff — Human-in-the-loop fallback for browser automation.

A standalone library that pauses your Playwright automation, hands the
page off to a human via CDP-based streaming, and resumes when they're
done — for OAuth, 2FA, payments, identity checks, or any flow that
requires a human.

Example:
    from browser_handoff import Handoff, Scenario
    from browser_handoff.detection import Detection

    handoff = Handoff(
        scenarios=[
            Scenario(
                name="login_required",
                trigger=Detection.element(present=['input[type="email"]']),
                complete=Detection.url(path_contains=["/dashboard"]),
            ),
        ],
    )

    result = await handoff.run(page)
    if result.was_blocked and not result.timed_out:
        print(f"Human completed: {result.scenario_name}")
    await bot_logic(page)
"""

from importlib.metadata import PackageNotFoundError, version

from .handoff import Handoff, HandoffResult
from .scenario import Scenario
from .server import ServerConfig

try:
    __version__ = version("browser-handoff")
except PackageNotFoundError:
    # Package is not installed (e.g. running from a checkout without `pip install -e .`).
    __version__ = "0.0.0+unknown"

__all__ = [
    "Handoff",
    "HandoffResult",
    "Scenario",
    "ServerConfig",
    "__version__",
]
