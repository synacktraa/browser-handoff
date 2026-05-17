"""Rich-rendered console notifier — used as the fallback default."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.panel import Panel

from .base import Notifier, Urgency

# force_terminal=True so colors render even when stdout is piped (e.g.,
# the script is running inside a Daytona sandbox and a parent process
# is forwarding the captured output to the operator's local terminal).
_console = Console(force_terminal=True)

_URGENCY_STYLES: dict[str, str] = {
    "low": "cyan",
    "normal": "blue",
    "high": "yellow",
    "critical": "bold yellow",
}


@dataclass
class ConsoleNotifier(Notifier):
    """Render the handoff banner as a rich panel on stdout.

    Used by Handoff as the fallback when the caller passes no notifiers,
    so the stream URL always lands somewhere obvious. Can also be added
    explicitly alongside other notifiers — useful if you want a panel on
    the developer terminal in addition to Slack/Discord pings.

    Example:
        notifier = ConsoleNotifier()
        # combined with another notifier:
        Handoff(scenarios=[...], notifiers=[notifier, SlackNotifier(...)])

    Serialization:
        {"type": "console"}
    """

    notifier_type: str = field(default="console", init=False)

    async def send(
        self,
        title: str,
        message: str,
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        style = _URGENCY_STYLES.get(urgency, "blue")
        _console.print()
        _console.print(
            Panel(
                message.strip(),
                title=f"[bold]{title}[/bold]",
                border_style=style,
                padding=(1, 2),
            )
        )
        _console.print()
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.notifier_type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConsoleNotifier":
        return cls()
