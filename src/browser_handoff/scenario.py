"""Scenario-based trigger-completion pairs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .detection import BaseDetection, Detection

if TYPE_CHECKING:
    pass


@dataclass
class Scenario:
    """A scenario defines a trigger-completion pair.

    When the trigger condition is detected, the handoff will wait for
    the corresponding completion condition. Only one scenario can be
    active at a time.

    Example:
        scenario = Scenario(
            name="cloudflare_challenge",
            trigger=Detection.content(title_contains=["Just a moment"]),
            complete=Detection.element(missing=[".cf-turnstile"]),
        )
    """

    name: str
    trigger: BaseDetection
    complete: BaseDetection

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "name": self.name,
            "trigger": self.trigger.to_dict(),
            "complete": self.complete.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        """Create from dictionary."""
        return cls(
            name=data.get("name", "unnamed"),
            trigger=Detection.from_dict(data["trigger"]),
            complete=Detection.from_dict(data["complete"]),
        )
