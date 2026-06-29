"""Base detection classes and result types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass
class DetectionResult:
    """Outcome of a detection check; truthy when matched."""

    matched: bool
    detection_type: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.matched


class BaseDetection(ABC):
    """Abstract base for all detection types.

    Detections are session-unaware: orchestration owns gating, lazy
    install, and presence checks. This keeps them drivable standalone.
    """

    detection_type: str = "base"

    @abstractmethod
    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Wire `callback` to fire whenever the detection should be re-checked.

        Args:
            page: Playwright page to observe.
            callback: Async function invoked with `self` when a re-check
                is due.

        Returns:
            A cleanup function that removes the registered listeners.
        """
        pass

    @abstractmethod
    async def check(self, page: "Page", **context: Any) -> DetectionResult:
        """Evaluate the condition against `page`.

        Args:
            page: Playwright page to inspect.
            **context: Per-call info from the orchestrator. Each detection
                reads only the keys it needs and ignores the rest;
                combinators forward unchanged.
        """
        pass

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.detection_type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BaseDetection":
        raise NotImplementedError("Subclasses must implement from_dict")
