"""Base detection classes and result types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from playwright.async_api import Page


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
