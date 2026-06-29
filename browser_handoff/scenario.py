"""Scenario-based trigger-completion pairs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .detection import Detection
from .detection.base import BaseDetection


@dataclass
class Scenario:
    """A trigger-completion pair for `Handoff.run`.

    Once `trigger` matches, the handoff waits for `complete` to match.

    Example:
        Scenario(
            name="login_required",
            trigger=Detection.url(path_contains=["/login"]),
            complete=Detection.url(path_contains=["/dashboard"]),
        )
    """

    name: str
    trigger: BaseDetection
    complete: BaseDetection

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trigger": self.trigger.to_dict(),
            "complete": self.complete.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        return cls(
            name=data.get("name", "unnamed"),
            trigger=Detection.from_dict(data["trigger"]),
            complete=Detection.from_dict(data["complete"]),
        )
