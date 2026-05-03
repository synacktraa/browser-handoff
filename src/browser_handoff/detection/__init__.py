"""Detection types and factory for browser-handoff."""

from __future__ import annotations

from typing import Any

from .base import BaseDetection, DetectionResult
from .combinators import AllDetection, AnyDetection, NotDetection
from .content import ContentDetection
from .element import ElementDetection
from .url import UrlDetection

# LLM detection is optional
try:
    from .llm import LLMDetection

    _HAS_LLM = True
except ImportError:
    _HAS_LLM = False
    LLMDetection = None  # type: ignore


class Detection:
    """Factory class for creating detection instances.

    Example:
        # Content detection
        detection = Detection.content(
            title_contains=["Just a moment"],
            body_contains=["challenges.cloudflare.com"],
        )

        # URL detection
        detection = Detection.url(
            host_equals=["localhost"],
            path_matches=["/callback"],
        )

        # Combinators
        detection = Detection.all([
            Detection.element(present=["#dashboard"]),
            Detection.not_(Detection.element(present=[".error"])),
        ])
    """

    @staticmethod
    def content(
        title_contains: list[str] | None = None,
        title_matches: list[str] | None = None,
        body_contains: list[str] | None = None,
        body_matches: list[str] | None = None,
    ) -> ContentDetection:
        """Create a content-based detection."""
        return ContentDetection(
            title_contains=title_contains or [],
            title_matches=title_matches or [],
            body_contains=body_contains or [],
            body_matches=body_matches or [],
        )

    @staticmethod
    def url(
        scheme_equals: str | None = None,
        host_equals: list[str] | None = None,
        host_not_equals: list[str] | None = None,
        path_matches: list[str] | None = None,
        path_contains: list[str] | None = None,
        query_contains: list[str] | None = None,
    ) -> UrlDetection:
        """Create a URL-based detection."""
        return UrlDetection(
            scheme_equals=scheme_equals,
            host_equals=host_equals or [],
            host_not_equals=host_not_equals or [],
            path_matches=path_matches or [],
            path_contains=path_contains or [],
            query_contains=query_contains or [],
        )

    @staticmethod
    def element(
        present: list[str] | None = None,
        missing: list[str] | None = None,
        visible: list[str] | None = None,
        hidden: list[str] | None = None,
    ) -> ElementDetection:
        """Create an element-based detection."""
        return ElementDetection(
            present=present or [],
            missing=missing or [],
            visible=visible or [],
            hidden=hidden or [],
        )

    @staticmethod
    def llm(
        model: str = "anthropic/claude-sonnet-4-20250514",
        condition: str = "",
    ) -> "LLMDetection":
        """Create an LLM-based detection.

        Requires the 'llm' extra: pip install browser-handoff[llm]
        """
        if not _HAS_LLM:
            raise ImportError(
                "LLM detection requires 'litellm' package. "
                "Install with: pip install browser-handoff[llm]"
            )
        return LLMDetection(model=model, condition=condition)

    @staticmethod
    def all(conditions: list[BaseDetection]) -> AllDetection:
        """Create an AND combinator (all conditions must match)."""
        return AllDetection(conditions=conditions)

    @staticmethod
    def any(conditions: list[BaseDetection]) -> AnyDetection:
        """Create an OR combinator (any condition must match)."""
        return AnyDetection(conditions=conditions)

    @staticmethod
    def not_(condition: BaseDetection) -> NotDetection:
        """Create a NOT combinator (invert condition)."""
        return NotDetection(condition=condition)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> BaseDetection:
        """Create a detection from dictionary representation.

        Args:
            data: Dictionary with 'type' key and type-specific fields.

        Returns:
            The appropriate detection instance.

        Raises:
            ValueError: If the detection type is unknown.
        """
        detection_type = data.get("type")

        if detection_type == "content":
            return ContentDetection.from_dict(data)
        elif detection_type == "url":
            return UrlDetection.from_dict(data)
        elif detection_type == "element":
            return ElementDetection.from_dict(data)
        elif detection_type == "llm":
            if not _HAS_LLM:
                raise ImportError(
                    "LLM detection requires 'litellm' package. "
                    "Install with: pip install browser-handoff[llm]"
                )
            return LLMDetection.from_dict(data)
        elif detection_type == "all":
            conditions = [Detection.from_dict(c) for c in data.get("conditions", [])]
            return AllDetection(conditions=conditions)
        elif detection_type == "any":
            conditions = [Detection.from_dict(c) for c in data.get("conditions", [])]
            return AnyDetection(conditions=conditions)
        elif detection_type == "not":
            condition_data = data.get("condition")
            condition = Detection.from_dict(condition_data) if condition_data else None
            return NotDetection(condition=condition)
        else:
            raise ValueError(f"Unknown detection type: {detection_type}")


__all__ = [
    "Detection",
    "DetectionResult",
    "BaseDetection",
    "ContentDetection",
    "UrlDetection",
    "ElementDetection",
    "AllDetection",
    "AnyDetection",
    "NotDetection",
]

# Conditionally export LLMDetection
if _HAS_LLM:
    __all__.append("LLMDetection")
