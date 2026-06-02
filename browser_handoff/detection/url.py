"""URL-based detection (scheme, host, path, query matching)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine
from urllib.parse import unquote, urlparse

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass
class UrlDetection(BaseDetection):
    """Detection based on URL components.

    Triggers on: page.on("framenavigated")

    Example:
        detection = UrlDetection(
            host_equals=["localhost", "accounts.google.com"],
            path_matches=["/callback"],
            query_contains=["code="],
        )
    """

    detection_type: str = field(default="url", init=False)

    scheme_equals: str | None = None
    host_equals: list[str] = field(default_factory=list)
    host_not_equals: list[str] = field(default_factory=list)
    path_matches: list[str] = field(default_factory=list)
    path_contains: list[str] = field(default_factory=list)
    query_contains: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Pre-compile path regex patterns
        self._path_patterns = [re.compile(p) for p in self.path_matches]

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register framenavigated listener."""
        loop = asyncio.get_running_loop()

        async def on_frame_navigated(frame: Any) -> None:
            if frame == page.main_frame:
                await callback(self)

        # The listener page.on() registers IS the lambda — store it so
        # remove_listener can find it. Using a module-level function
        # reference would point at a different object than what was added.
        listener = lambda frame: loop.create_task(on_frame_navigated(frame))
        page.on("framenavigated", listener)

        def cleanup() -> None:
            try:
                page.remove_listener("framenavigated", listener)
            except Exception:
                pass

        return cleanup

    async def check(self, page: "Page") -> DetectionResult:
        """Check if current URL matches conditions."""
        try:
            url = page.url
            parsed = urlparse(url)
        except Exception as e:
            return DetectionResult(
                matched=False,
                detection_type=self.detection_type,
                reason=f"Failed to parse URL: {e}",
            )

        # Check scheme_equals
        if self.scheme_equals is not None:
            if parsed.scheme != self.scheme_equals:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Scheme '{parsed.scheme}' does not equal '{self.scheme_equals}'",
                )

        # Check host_equals (any match)
        if self.host_equals:
            if parsed.hostname not in self.host_equals:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Host '{parsed.hostname}' not in {self.host_equals}",
                )

        # Check host_not_equals (none should match)
        if self.host_not_equals:
            if parsed.hostname in self.host_not_equals:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Host '{parsed.hostname}' is in exclusion list",
                )

        # Check path_matches (any regex match)
        if self.path_matches:
            matched = False
            for i, pattern in enumerate(self._path_patterns):
                if pattern.search(parsed.path):
                    matched = True
                    break
            if not matched:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Path '{parsed.path}' does not match any patterns",
                )

        # Check path_contains (any substring match)
        if self.path_contains:
            matched = False
            for substring in self.path_contains:
                if substring in parsed.path:
                    matched = True
                    break
            if not matched:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Path '{parsed.path}' does not contain any patterns",
                )

        # Check query_contains (all must be present)
        if self.query_contains:
            query = parsed.query or ""
            for substring in self.query_contains:
                if substring not in query:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Query does not contain '{substring}'",
                    )

        # All conditions passed. unquote keeps the URL human-readable in
        # the reason string — raw percent-encoded URLs (full of %2F, %3A,
        # %3D) are unreadable in the breadcrumb. `details["url"]` keeps
        # the raw form for programmatic consumers.
        return DetectionResult(
            matched=True,
            detection_type=self.detection_type,
            reason=f"URL '{unquote(url)}' matches all conditions",
            details={"url": url},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        result: dict[str, Any] = {"type": self.detection_type}
        if self.scheme_equals:
            result["scheme_equals"] = self.scheme_equals
        if self.host_equals:
            result["host_equals"] = self.host_equals
        if self.host_not_equals:
            result["host_not_equals"] = self.host_not_equals
        if self.path_matches:
            result["path_matches"] = self.path_matches
        if self.path_contains:
            result["path_contains"] = self.path_contains
        if self.query_contains:
            result["query_contains"] = self.query_contains
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UrlDetection":
        """Create from dictionary."""
        return cls(
            scheme_equals=data.get("scheme_equals"),
            host_equals=data.get("host_equals", []),
            host_not_equals=data.get("host_not_equals", []),
            path_matches=data.get("path_matches", []),
            path_contains=data.get("path_contains", []),
            query_contains=data.get("query_contains", []),
        )
