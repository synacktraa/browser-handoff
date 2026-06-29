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
    """Match on URL components (scheme, host, path, query).

    All configured clauses are AND. Fires on main-frame navigation.

    Example:
        UrlDetection(
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
        self._path_patterns = [re.compile(p) for p in self.path_matches]

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Fire the callback on every main-frame navigation.

        Args:
            page: Playwright page to observe.
            callback: Async function invoked with `self`.

        Returns:
            A cleanup function that removes the listener.
        """
        loop = asyncio.get_running_loop()

        async def on_frame_navigated(frame: Any) -> None:
            if frame == page.main_frame:
                await callback(self)

        # Store the lambda so remove_listener can match by identity.
        listener = lambda frame: loop.create_task(on_frame_navigated(frame))
        page.on("framenavigated", listener)

        def cleanup() -> None:
            try:
                page.remove_listener("framenavigated", listener)
            except Exception:
                pass

        return cleanup

    async def check(self, page: "Page", **context: Any) -> DetectionResult:
        """Return a match only when every configured clause holds.

        Args:
            page: Playwright page to inspect.
            **context: Unused.
        """
        try:
            url = page.url
            parsed = urlparse(url)
        except Exception as e:
            return DetectionResult(
                matched=False,
                detection_type=self.detection_type,
                reason=f"Failed to parse URL: {e}",
            )

        # Name the matching clauses in `reason` so logs surface which
        # rules fired.
        matched_clauses: list[str] = []

        if self.scheme_equals is not None:
            if parsed.scheme != self.scheme_equals:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Scheme '{parsed.scheme}' does not equal '{self.scheme_equals}'",
                )
            matched_clauses.append(f"scheme_equals matched '{self.scheme_equals}'")

        if self.host_equals:
            if parsed.hostname not in self.host_equals:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Host '{parsed.hostname}' not in {self.host_equals}",
                )
            matched_clauses.append(f"host_equals matched '{parsed.hostname}'")

        if self.host_not_equals:
            if parsed.hostname in self.host_not_equals:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Host '{parsed.hostname}' is in exclusion list",
                )
            matched_clauses.append(
                f"host_not_equals matched (host '{parsed.hostname}' not excluded)"
            )

        if self.path_matches:
            hit: str | None = None
            for i, pattern in enumerate(self._path_patterns):
                if pattern.search(parsed.path):
                    hit = self.path_matches[i]
                    break
            if hit is None:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Path '{parsed.path}' does not match any patterns",
                )
            matched_clauses.append(f"path_matches matched pattern '{hit}'")

        if self.path_contains:
            hit_substring: str | None = None
            for substring in self.path_contains:
                if substring in parsed.path:
                    hit_substring = substring
                    break
            if hit_substring is None:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason=f"Path '{parsed.path}' does not contain any patterns",
                )
            matched_clauses.append(f"path_contains matched '{hit_substring}'")

        if self.query_contains:
            query = parsed.query or ""
            for substring in self.query_contains:
                if substring not in query:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Query does not contain '{substring}'",
                    )
            matched_clauses.append(
                "query_contains matched " + ", ".join(f"'{s}'" for s in self.query_contains)
            )

        # `reason` shows the decoded URL for humans; `details["url"]`
        # keeps the raw form for programmatic consumers.
        if matched_clauses:
            reason = f"URL '{unquote(url)}' matches: " + ", ".join(matched_clauses)
        else:
            reason = f"URL '{unquote(url)}' matches (no conditions configured)"

        return DetectionResult(
            matched=True,
            detection_type=self.detection_type,
            reason=reason,
            details={"url": url},
        )

    def to_dict(self) -> dict[str, Any]:
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
        return cls(
            scheme_equals=data.get("scheme_equals"),
            host_equals=data.get("host_equals", []),
            host_not_equals=data.get("host_not_equals", []),
            path_matches=data.get("path_matches", []),
            path_contains=data.get("path_contains", []),
            query_contains=data.get("query_contains", []),
        )
