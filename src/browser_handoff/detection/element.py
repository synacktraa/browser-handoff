"""Element-based detection (DOM element presence/absence/visibility)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page


# JavaScript to inject MutationObserver
MUTATION_OBSERVER_SCRIPT = """
(function() {
    if (window.__handoffMutationObserver) return;

    let debounceTimer = null;
    const DEBOUNCE_MS = 100;

    const observer = new MutationObserver((mutations) => {
        // Debounce to avoid excessive callbacks
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
            window.__handoffMutationCallback && window.__handoffMutationCallback();
        }, DEBOUNCE_MS);
    });

    observer.observe(document.body || document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['class', 'style', 'hidden', 'disabled']
    });

    window.__handoffMutationObserver = observer;
})();
"""


@dataclass
class ElementDetection(BaseDetection):
    """Detection based on DOM element presence, absence, or visibility.

    Triggers on: MutationObserver via CDP (significant DOM changes)

    Example:
        detection = ElementDetection(
            present=[".captcha-container", "#challenge-form"],
            missing=["button#submit", ".main-content"],
            visible=[".modal-overlay"],
            hidden=[".loading-spinner"],
        )
    """

    detection_type: str = field(default="element", init=False)

    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    visible: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register MutationObserver via page evaluation."""
        cleanup_called = False
        callback_registered = False

        async def setup_observer() -> None:
            nonlocal callback_registered
            if callback_registered:
                return

            try:
                # Inject the mutation observer script
                await page.evaluate(MUTATION_OBSERVER_SCRIPT)

                # Expose callback function
                async def on_mutation() -> None:
                    if not cleanup_called:
                        await callback(self)

                await page.expose_function("__handoffMutationCallback", on_mutation)
                callback_registered = True
            except Exception:
                # Page might be navigating, observer will be re-setup
                pass

        # Setup observer now and on each navigation
        async def on_dom_content_loaded() -> None:
            nonlocal callback_registered
            callback_registered = False
            await setup_observer()
            # Also trigger a check on page load
            if not cleanup_called:
                await callback(self)

        # Initial setup
        page._loop.create_task(setup_observer())

        # Re-setup on navigation
        page.on("domcontentloaded", lambda: page._loop.create_task(on_dom_content_loaded()))

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True
            try:
                page.remove_listener("domcontentloaded", on_dom_content_loaded)
            except Exception:
                pass

        return cleanup

    async def check(self, page: "Page") -> DetectionResult:
        """Check if element conditions are met."""
        try:
            # Check 'present' selectors - all must exist
            for selector in self.present:
                element = await page.query_selector(selector)
                if element is None:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Required element '{selector}' not present",
                    )

            # Check 'missing' selectors - none should exist
            for selector in self.missing:
                element = await page.query_selector(selector)
                if element is not None:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Element '{selector}' should be missing but exists",
                    )

            # Check 'visible' selectors - all must be visible
            for selector in self.visible:
                element = await page.query_selector(selector)
                if element is None:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Visible element '{selector}' not found",
                    )
                is_visible = await element.is_visible()
                if not is_visible:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Element '{selector}' exists but is not visible",
                    )

            # Check 'hidden' selectors - all must be hidden or not exist
            for selector in self.hidden:
                element = await page.query_selector(selector)
                if element is not None:
                    is_visible = await element.is_visible()
                    if is_visible:
                        return DetectionResult(
                            matched=False,
                            detection_type=self.detection_type,
                            reason=f"Element '{selector}' should be hidden but is visible",
                        )

            # All conditions met
            return DetectionResult(
                matched=True,
                detection_type=self.detection_type,
                reason="All element conditions met",
                details={
                    "present": self.present,
                    "missing": self.missing,
                    "visible": self.visible,
                    "hidden": self.hidden,
                },
            )

        except Exception as e:
            return DetectionResult(
                matched=False,
                detection_type=self.detection_type,
                reason=f"Failed to check elements: {e}",
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        result: dict[str, Any] = {"type": self.detection_type}
        if self.present:
            result["present"] = self.present
        if self.missing:
            result["missing"] = self.missing
        if self.visible:
            result["visible"] = self.visible
        if self.hidden:
            result["hidden"] = self.hidden
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ElementDetection":
        """Create from dictionary."""
        return cls(
            present=data.get("present", []),
            missing=data.get("missing", []),
            visible=data.get("visible", []),
            hidden=data.get("hidden", []),
        )
