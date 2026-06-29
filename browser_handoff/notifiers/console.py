"""Rich-rendered console notifier — used as the fallback default."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.panel import Panel

from .base import Notifier, Urgency
from .message import LinkItem, MessageItem, TextItem

# force_terminal=True so colors render when stdout is piped (e.g.,
# a sandbox forwarding captured output to the operator's terminal).
_console = Console(force_terminal=True)

_URGENCY_STYLES: dict[str, str] = {
    "low": "cyan",
    "normal": "blue",
    "high": "yellow",
    "critical": "bold yellow",
}


@dataclass
class ConsoleNotifier(Notifier):
    """Render the handoff banner as a Rich panel on stdout.

    `LinkItem`s become `[link=…]` markup so OSC 8 terminals (iTerm2,
    Windows Terminal, Kitty, Alacritty) make them Ctrl/Cmd-clickable
    and expose "Copy Link Address" on right-click.

    Used as the fallback when the caller passes no notifiers; can also
    be added alongside others.

    Example:
        Handoff(notifiers=[ConsoleNotifier(), SlackNotifier(...)])

    Serialization:
        {"type": "console"}
    """

    notifier_type: str = field(default="console", init=False)

    async def send(
        self,
        title: str,
        message: str | list[MessageItem],
        urgency: Urgency = "normal",
        **kwargs: Any,
    ) -> bool:
        style = _URGENCY_STYLES.get(urgency, "blue")
        items = self._normalize_items(message)

        rendered: list[str] = []
        for item in items:
            if isinstance(item, TextItem):
                rendered.append(item.text)
            elif isinstance(item, LinkItem):
                rendered.append(
                    f"{item.prefix}"
                    f"[link={item.url}]{item.url}[/link]"
                    f"{item.suffix}"
                )

        _console.print()
        _console.print(
            Panel(
                "\n\n".join(rendered),
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
