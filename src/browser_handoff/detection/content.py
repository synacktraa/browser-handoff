"""Content-based detection (title and body content matching)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass
class ContentDetection(BaseDetection):
    """Detection based on page title or body content.

    Triggers on: page.on("domcontentloaded")

    Example:
        detection = ContentDetection(
            title_contains=["Just a moment", "Access Denied"],
            body_contains=["challenges.cloudflare.com"],
        )
    """

    detection_type: str = field(default="content", init=False)

    title_contains: list[str] = field(default_factory=list)
    title_matches: list[str] = field(default_factory=list)
    body_contains: list[str] = field(default_factory=list)
    body_matches: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Pre-compile regex patterns
        self._title_patterns = [re.compile(p) for p in self.title_matches]
        self._body_patterns = [re.compile(p) for p in self.body_matches]

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register domcontentloaded listener."""

        async def on_dom_content_loaded() -> None:
            await callback(self)

        page.on("domcontentloaded", lambda: page._loop.create_task(on_dom_content_loaded()))

        def cleanup() -> None:
            try:
                page.remove_listener("domcontentloaded", on_dom_content_loaded)
            except Exception:
                pass

        return cleanup

    async def check(self, page: "Page") -> DetectionResult:
        """Check if page content matches any conditions."""
        try:
            title = await page.title()
            body = await page.content()
        except Exception as e:
            return DetectionResult(
                matched=False,
                detection_type=self.detection_type,
                reason=f"Failed to get page content: {e}",
            )

        # Check title_contains
        for pattern in self.title_contains:
            if pattern in title:
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Title contains '{pattern}'",
                    details={"matched_pattern": pattern, "match_type": "title_contains"},
                )

        # Check title_matches (regex)
        for i, pattern in enumerate(self._title_patterns):
            if pattern.search(title):
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Title matches pattern '{self.title_matches[i]}'",
                    details={"matched_pattern": self.title_matches[i], "match_type": "title_matches"},
                )

        # Check body_contains
        for pattern in self.body_contains:
            if pattern in body:
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Body contains '{pattern}'",
                    details={"matched_pattern": pattern, "match_type": "body_contains"},
                )

        # Check body_matches (regex)
        for i, pattern in enumerate(self._body_patterns):
            if pattern.search(body):
                return DetectionResult(
                    matched=True,
                    detection_type=self.detection_type,
                    reason=f"Body matches pattern '{self.body_matches[i]}'",
                    details={"matched_pattern": self.body_matches[i], "match_type": "body_matches"},
                )

        return DetectionResult(
            matched=False,
            detection_type=self.detection_type,
            reason="No content patterns matched",
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
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
        """Create from dictionary."""
        return cls(
            title_contains=data.get("title_contains", []),
            title_matches=data.get("title_matches", []),
            body_contains=data.get("body_contains", []),
            body_matches=data.get("body_matches", []),
        )
