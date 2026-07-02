"""Scenario — a start / until pair for `Handoff.guard`."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

from .detection import Detection
from .detection.base import BaseDetection


@dataclass(init=False)
class Scenario:
    """An `on`-detection paired with an `until`-detection for `Handoff.guard`.

    Once `on` matches, the handoff waits for `until` to match.

    Example:
        Scenario(
            name="login_required",
            on=Detection.url(path_contains=["/login"]),
            until=Detection.url(path_contains=["/dashboard"]),
        )
    """

    name: str
    on: BaseDetection
    until: BaseDetection

    def __init__(
        self,
        name: str,
        on: BaseDetection | None = None,
        until: BaseDetection | None = None,
        *,
        trigger: BaseDetection | None = None,
        complete: BaseDetection | None = None,
    ) -> None:
        if trigger is not None:
            warnings.warn(
                "Scenario(trigger=...) is deprecated; use `on=...`. "
                "Will be removed in a future major release.",
                DeprecationWarning,
                stacklevel=2,
            )
            if on is None:
                on = trigger
        if complete is not None:
            warnings.warn(
                "Scenario(complete=...) is deprecated; use `until=...`. "
                "Will be removed in a future major release.",
                DeprecationWarning,
                stacklevel=2,
            )
            if until is None:
                until = complete
        if on is None:
            raise TypeError("Scenario requires `on` (or the deprecated `trigger`).")
        if until is None:
            raise TypeError("Scenario requires `until` (or the deprecated `complete`).")
        self.name = name
        self.on = on
        self.until = until

    @property
    def trigger(self) -> BaseDetection:
        """Deprecated alias for :attr:`on`."""
        warnings.warn(
            "Scenario.trigger is deprecated; use `.on`. "
            "Will be removed in a future major release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.on

    @property
    def complete(self) -> BaseDetection:
        """Deprecated alias for :attr:`until`."""
        warnings.warn(
            "Scenario.complete is deprecated; use `.until`. "
            "Will be removed in a future major release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.until

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "on": self.on.to_dict(),
            "until": self.until.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        kwargs: dict[str, Any] = {"name": data.get("name", "unnamed")}
        # New keys take precedence; fall back to deprecated ones with a
        # warning so config files stay accepted.
        if "on" in data:
            kwargs["on"] = Detection.from_dict(data["on"])
        elif "trigger" in data:
            warnings.warn(
                "Scenario dict key `trigger` is deprecated; use `on`. "
                "Will be removed in a future major release.",
                DeprecationWarning,
                stacklevel=2,
            )
            kwargs["on"] = Detection.from_dict(data["trigger"])
        if "until" in data:
            kwargs["until"] = Detection.from_dict(data["until"])
        elif "complete" in data:
            warnings.warn(
                "Scenario dict key `complete` is deprecated; use `until`. "
                "Will be removed in a future major release.",
                DeprecationWarning,
                stacklevel=2,
            )
            kwargs["until"] = Detection.from_dict(data["complete"])
        return cls(**kwargs)
