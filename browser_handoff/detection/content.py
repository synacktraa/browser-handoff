"""Content-based detection (title and body content matching)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass
class ContentDetection(BaseDetection):
    """Match on page title or full-document body content.

    Fires on `domcontentloaded`. Each clause is OR within its kind
    (any title_contains hit, any body_matches hit, …); a single hit
    across any clause is a match.

    Example:
        ContentDetection(
            title_contains=["Sign In", "Login Required"],
            body_contains=["please enter your password"],
        )
    """

    detection_type: str = field(default="content", init=False)

    title_contains: list[str] = field(default_factory=list)
    title_matches: list[str] = field(default_factory=list)
    body_contains: list[str] = field(default_factory=list)
    body_matches: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._title_patterns = [re.compile(p) for p in self.title_matches]
        self._body_patterns = [re.compile(p) for p in self.body_matches]

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Fire the callback on every `domcontentloaded`.

        Args:
            page: Playwright page to observe.
            callback: Async function invoked with `self` after each load.

        Returns:
            A cleanup function that removes the listener.
        """
        loop = asyncio.get_running_loop()

        async def on_dom_content_loaded() -> None:
            await callback(self)

        listener = lambda: loop.create_task(on_dom_content_loaded())
        page.on("domcontentloaded", listener)

        def cleanup() -> None:
            try:
                page.remove_listener("domcontentloaded", listener)
            except Exception:
                pass

        return cleanup

    async def check(self, page: "Page", **context: Any) -> DetectionResult:
        """Return a match on the first clause that hits title or body.

        Args:
            page: Playwright page to inspect.
            **context: Unused.
        """
        try:
            title = await page.title()
            body = await page.content()
        except Exception as e:
            return DetectionResult(
                matched=False,
                detection_type=self.detection_type,
                reason=f"Failed to get page content: {e}",
            )

        # Name the matching clause in `reason` so logs/notifications
        # surface which rule fired.
        for pattern in self.title_contains:
            if pattern in title:
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Title matched: title_contains '{pattern}'",
                    details={"matched_pattern": pattern, "match_type": "title_contains"},
                )

        for i, pattern in enumerate(self._title_patterns):
            if pattern.search(title):
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Title matched: title_matches pattern '{self.title_matches[i]}'",
                    details={"matched_pattern": self.title_matches[i], "match_type": "title_matches"},
                )

        for pattern in self.body_contains:
            if pattern in body:
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Body matched: body_contains '{pattern}'",
                    details={"matched_pattern": pattern, "match_type": "body_contains"},
                )

        for i, pattern in enumerate(self._body_patterns):
            if pattern.search(body):
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Body matched: body_matches pattern '{self.body_matches[i]}'",
                    details={"matched_pattern": self.body_matches[i], "match_type": "body_matches"},
                )

        return DetectionResult(
            matched=False,
            detection_type=self.detection_type,
            reason="No content patterns matched",
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.detection_type}
        if self.title_contains:
            result["title_contains"] = self.title_contains
        if self.title_matches:
            result["title_matches"] = self.title_matches
        if self.body_contains:
            result["body_contains"] = self.body_contains
        if self.body_matches:
            result["body_matches"] = self.body_matches
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContentDetection":
        return cls(
            title_contains=data.get("title_contains", []),
            title_matches=data.get("title_matches", []),
            body_contains=data.get("body_contains", []),
            body_matches=data.get("body_matches", []),
        )
