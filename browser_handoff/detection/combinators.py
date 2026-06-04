"""Combinator detection types (all, any, not)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ..server.session import HandoffSession


_AND_HEADER = "Matched conditions:\n"
_AND_BULLET = "• "


def _flatten_and_items(reason: str) -> list[str]:
    """Return child reasons of an AND-shaped reason, or [reason] otherwise.

    AND nested in AND is semantically the same as one flat AND, so its bullet
    items get spliced into the outer list instead of producing a nested
    "Matched conditions:" header.
    """
    if not reason.startswith(_AND_HEADER):
        return [reason]
    body = reason[len(_AND_HEADER):]
    items: list[str] = []
    for line in body.split("\n"):
        if line.startswith(_AND_BULLET):
            items.append(line[len(_AND_BULLET):])
    return items or [reason]


@dataclass
class AllDetection(BaseDetection):
    """AND logic - all conditions must match.

    Example:
        detection = AllDetection(conditions=[
            UrlDetection(path_matches=["/dashboard"]),
            ElementDetection(present=[".user-avatar"]),
        ])
    """

    detection_type: str = field(default="all", init=False)
    conditions: list[BaseDetection] = field(default_factory=list)

    def bind(self, *, session: "HandoffSession | None" = None) -> None:
        """Forward to each child so nested LLMDetections still get gated."""
        for condition in self.conditions:
            condition.bind(session=session)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register listeners for all child conditions."""
        cleanups: list[Callable[[], None]] = []

        for condition in self.conditions:
            cleanup = condition.register_listeners(page, callback)
            cleanups.append(cleanup)

        def cleanup_all() -> None:
            for cleanup in cleanups:
                try:
                    cleanup()
                except Exception:
                    pass

        return cleanup_all

    async def check(self, page: "Page") -> DetectionResult:
        """Check if ALL conditions are met."""
        child_reasons: list[str] = []
        for condition in self.conditions:
            result = await condition.check(page)
            if not result.matched:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Condition '{condition.detection_type}' not met: {result.reason}",
                    details={"failed_condition": condition.to_dict()},
                )
            # Flatten nested AND: an AND inside an AND is semantically the
            # same as one flat AND, so its bullet items contribute directly
            # rather than producing a nested "Matched conditions:" header.
            child_reasons.extend(_flatten_and_items(result.reason))

        if child_reasons:
            reason = "Matched conditions:\n" + "\n".join(
                f"• {r}" for r in child_reasons
            )
        else:
            reason = "All conditions met (no conditions configured)"

        return DetectionResult(
            matched=True,
            detection_type=self.detection_type,
            reason=reason,
            details={"conditions_count": len(self.conditions)},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "type": self.detection_type,
            "conditions": [c.to_dict() for c in self.conditions],
        }


@dataclass
class AnyDetection(BaseDetection):
    """OR logic - any condition must match.

    Example:
        detection = AnyDetection(conditions=[
            ElementDetection(present=["#success"]),
            ContentDetection(body_contains=["Welcome"]),
        ])
    """

    detection_type: str = field(default="any", init=False)
    conditions: list[BaseDetection] = field(default_factory=list)

    def bind(self, *, session: "HandoffSession | None" = None) -> None:
        """Forward to each child so nested LLMDetections still get gated."""
        for condition in self.conditions:
            condition.bind(session=session)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register listeners for all child conditions."""
        cleanups: list[Callable[[], None]] = []

        for condition in self.conditions:
            cleanup = condition.register_listeners(page, callback)
            cleanups.append(cleanup)

        def cleanup_all() -> None:
            for cleanup in cleanups:
                try:
                    cleanup()
                except Exception:
                    pass

        return cleanup_all

    async def check(self, page: "Page") -> DetectionResult:
        """Check if ANY condition is met."""
        for condition in self.conditions:
            result = await condition.check(page)
            if result.matched:
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Condition '{condition.detection_type}' matched: {result.reason}",
                    details={"matched_condition": condition.to_dict()},
                )

        return DetectionResult(
            matched=False,
            detection_type=self.detection_type,
            reason="No conditions matched",
            details={"conditions_count": len(self.conditions)},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "type": self.detection_type,
            "conditions": [c.to_dict() for c in self.conditions],
        }


@dataclass
class NotDetection(BaseDetection):
    """Invert result of a condition.

    Example:
        detection = NotDetection(
            condition=ElementDetection(present=[".error-message"])
        )
    """

    detection_type: str = field(default="not", init=False)
    condition: BaseDetection | None = None

    def bind(self, *, session: "HandoffSession | None" = None) -> None:
        """Forward to the wrapped child so a nested LLMDetection still gets gated."""
        if self.condition is not None:
            self.condition.bind(session=session)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register listeners for the wrapped condition."""
        if self.condition is None:
            return lambda: None

        return self.condition.register_listeners(page, callback)

    async def check(self, page: "Page") -> DetectionResult:
        """Check if condition is NOT met."""
        if self.condition is None:
            return DetectionResult(
                matched=True,
                detection_type=self.detection_type,
                reason="No condition to negate",
            )

        result = await self.condition.check(page)

        if result.matched:
            return DetectionResult(
                matched=False,
                detection_type=self.detection_type,
                reason=f"Condition matched (should not): {result.reason}",
                details={"negated_condition": self.condition.to_dict()},
            )
        else:
            return DetectionResult(
                matched=True,
                detection_type=self.detection_type,
                reason=f"Condition not matched (as expected): {result.reason}",
                details={"negated_condition": self.condition.to_dict()},
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "type": self.detection_type,
            "condition": self.condition.to_dict() if self.condition else None,
        }
