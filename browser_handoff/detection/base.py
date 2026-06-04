"""Base detection classes and result types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ..server.session import HandoffSession


@dataclass
class DetectionResult:
    """Result of a detection check."""

    matched: bool
    detection_type: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.matched


class BaseDetection(ABC):
    """Abstract base class for all detection types."""

    detection_type: str = "base"

    def bind(self, *, session: "HandoffSession | None" = None) -> None:
        """Receive the per-handoff session before register_listeners runs.

        Called by Handoff.wait_for_completion immediately after a session is
        registered. Default is a no-op — cheap, page-driven detections
        (URL/Element/Content) don't need anything from the session.

        Subclasses override to read whatever they need: LLMDetection picks
        up `session.operator_activity` (gate vision calls on operator
        presence, not page noise) and `session.reason` (the trigger-time
        explanation the agent gave the human, which is much more
        informative for the model than the bare `condition` alone — the
        condition is often the agent's over-specific guess at the resume
        state, while reason names the actual task). Combinator detections
        forward bind() to their children so a nested LLMDetection still
        sees the session.

        Detections used standalone (no Handoff) simply never have bind()
        called and fall back to their pre-bound behavior.
        """
        return

    @abstractmethod
    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register event listeners that call callback when detection should be checked.

        Args:
            page: The Playwright page to monitor.
            callback: Async callback to invoke when detection should be checked.

        Returns:
            A cleanup function that removes the registered listeners.
        """
        pass

    @abstractmethod
    async def check(self, page: "Page") -> DetectionResult:
        """Check if detection condition is met.

        Args:
            page: The Playwright page to check.

        Returns:
            DetectionResult indicating whether condition was matched.
        """
        pass

    def to_dict(self) -> dict[str, Any]:
        """Serialize detection to dictionary format."""
        return {"type": self.detection_type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BaseDetection":
        """Deserialize detection from dictionary format.

        This should be overridden by subclasses.
        """
        raise NotImplementedError("Subclasses must implement from_dict")
