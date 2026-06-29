"""Combinator detection types (all, any, not)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_AND_HEADER = "Matched conditions:\n"
_AND_BULLET = "• "


def _flatten_and_items(reason: str) -> list[str]:
    """Splice bullets from a nested AND reason into the outer list.

    A flat AND inside an AND would render as a nested "Matched conditions:"
    block; flattening keeps the output one level deep.
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
    """AND: all conditions must match.

    Example:
        AllDetection(conditions=[
            UrlDetection(path_matches=["/dashboard"]),
            ElementDetection(present=[".user-avatar"]),
        ])
    """

    detection_type: str = field(default="all", init=False)
    conditions: list[BaseDetection] = field(default_factory=list)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register every child's listeners; forward the combinator on fire.

        The callback receives `self` (not the child whose listener
        actually fired), so the orchestrator runs its `check` against the
        combinator and re-evaluates AND semantics.

        Args:
            page: Playwright page to observe.
            callback: Async function invoked with `self` when any child
                fires.

        Returns:
            A cleanup function that tears down every child's listeners.
        """
        cleanups: list[Callable[[], None]] = []

        async def on_child_event(_child: BaseDetection) -> None:
            await callback(self)

        for condition in self.conditions:
            cleanup = condition.register_listeners(page, on_child_event)
            cleanups.append(cleanup)

        def cleanup_all() -> None:
            for cleanup in cleanups:
                try:
                    cleanup()
                except Exception:
                    pass

        return cleanup_all

    async def check(self, page: "Page", **context: Any) -> DetectionResult:
        """Return a match only when every child matches.

        Args:
            page: Playwright page to inspect.
            **context: Forwarded verbatim to each child's `check`.
        """
        child_reasons: list[str] = []
        for condition in self.conditions:
            result = await condition.check(page, **context)
            if not result.matched:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Condition '{condition.detection_type}' not met: {result.reason}",
                    details={"failed_condition": condition.to_dict()},
                )
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
        return {
            "type": self.detection_type,
            "conditions": [c.to_dict() for c in self.conditions],
        }


@dataclass
class AnyDetection(BaseDetection):
    """OR: any condition matches.

    Example:
        AnyDetection(conditions=[
            ElementDetection(present=["#success"]),
            ContentDetection(body_contains=["Welcome"]),
        ])
    """

    detection_type: str = field(default="any", init=False)
    conditions: list[BaseDetection] = field(default_factory=list)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register every child's listeners; forward the combinator on fire.

        The callback receives `self` so the orchestrator's logs and
        `check` report the OR's framing instead of the child's.

        Args:
            page: Playwright page to observe.
            callback: Async function invoked with `self` when any child
                fires.

        Returns:
            A cleanup function that tears down every child's listeners.
        """
        cleanups: list[Callable[[], None]] = []

        async def on_child_event(_child: BaseDetection) -> None:
            await callback(self)

        for condition in self.conditions:
            cleanup = condition.register_listeners(page, on_child_event)
            cleanups.append(cleanup)

        def cleanup_all() -> None:
            for cleanup in cleanups:
                try:
                    cleanup()
                except Exception:
                    pass

        return cleanup_all

    async def check(self, page: "Page", **context: Any) -> DetectionResult:
        """Return a match on the first matching child.

        Args:
            page: Playwright page to inspect.
            **context: Forwarded verbatim to each child's `check`.
        """
        for condition in self.conditions:
            result = await condition.check(page, **context)
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
        return {
            "type": self.detection_type,
            "conditions": [c.to_dict() for c in self.conditions],
        }


@dataclass
class NotDetection(BaseDetection):
    """NOT: invert the inner condition.

    Example:
        NotDetection(condition=ElementDetection(present=[".error-message"]))
    """

    detection_type: str = field(default="not", init=False)
    condition: BaseDetection | None = None

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register the inner condition's listeners; forward `self` on fire.

        The orchestrator's `check` runs against `self`, so the
        inversion isn't bypassed. A `None` inner is a no-op.

        Args:
            page: Playwright page to observe.
            callback: Async function invoked with `self` when the inner
                condition fires.

        Returns:
            A cleanup function (no-op if `condition` is None).
        """
        if self.condition is None:
            return lambda: None

        async def on_child_event(_child: BaseDetection) -> None:
            await callback(self)

        return self.condition.register_listeners(page, on_child_event)

    async def check(self, page: "Page", **context: Any) -> DetectionResult:
        """Return a match when the inner condition does NOT match.

        A `None` inner trivially matches.

        Args:
            page: Playwright page to inspect.
            **context: Forwarded verbatim to the inner condition's `check`.
        """
        if self.condition is None:
            return DetectionResult(
                matched=True,
                detection_type=self.detection_type,
                reason="No condition to negate",
            )

        result = await self.condition.check(page, **context)

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
        return {
            "type": self.detection_type,
            "condition": self.condition.to_dict() if self.condition else None,
        }
