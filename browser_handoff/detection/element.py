"""Element-based detection (DOM element presence/absence/visibility)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page


# Idempotent observer install: re-injected on every navigation, but the
# JS-side flag prevents double-binding within a page context.
MUTATION_OBSERVER_SCRIPT = """
(function() {
    if (window.__handoffMutationObserver) return;

    let debounceTimer = null;
    const DEBOUNCE_MS = 100;

    const observer = new MutationObserver(() => {
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

# Per-page state attached to the Page instance so that multiple
# ElementDetection instances share one MutationObserver + one
# expose_function call (which Playwright disallows registering twice
# under the same name).
_DISPATCHER_ATTR = "_browser_handoff_mutation_dispatcher"


class _MutationDispatcher:
    """Fan-out for the page-wide MutationObserver callback."""

    def __init__(self, page: "Page") -> None:
        self.page = page
        self.callbacks: list[Callable[[], Awaitable[None]]] = []
        self.dom_listener: Callable[[], None] | None = None

    async def fire(self) -> None:
        for cb in list(self.callbacks):
            try:
                await cb()
            except Exception:
                pass


def _get_or_create_dispatcher(
    page: "Page", loop: asyncio.AbstractEventLoop
) -> tuple[_MutationDispatcher, bool]:
    """Return (dispatcher, is_first). is_first triggers one-time setup."""
    existing = getattr(page, _DISPATCHER_ATTR, None)
    if existing is not None:
        return existing, False
    dispatcher = _MutationDispatcher(page)
    setattr(page, _DISPATCHER_ATTR, dispatcher)
    return dispatcher, True


@dataclass
class ElementDetection(BaseDetection):
    """Detection based on DOM element presence, absence, or visibility.

    Triggers on: page-wide MutationObserver, fanned out via a single
    expose_function bound per page.

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
        loop = asyncio.get_running_loop()
        cleanup_called = False

        async def my_callback() -> None:
            if not cleanup_called:
                await callback(self)

        dispatcher, is_first = _get_or_create_dispatcher(page, loop)
        dispatcher.callbacks.append(my_callback)

        if is_first:
            async def setup() -> None:
                try:
                    await page.evaluate(MUTATION_OBSERVER_SCRIPT)
                except Exception:
                    pass
                try:
                    await page.expose_function(
                        "__handoffMutationCallback", dispatcher.fire
                    )
                except Exception:
                    # Already exposed (e.g. handoff.run called twice on the
                    # same page after a previous teardown). The dispatcher
                    # we just stored is the live one — JS will call it.
                    pass

            loop.create_task(setup())

            # Re-inject the JS observer on every navigation; trigger one
            # check per load so newly-rendered DOM is evaluated.
            async def on_navigate() -> None:
                try:
                    await page.evaluate(MUTATION_OBSERVER_SCRIPT)
                except Exception:
                    pass
                await dispatcher.fire()

            dom_listener = lambda: loop.create_task(on_navigate())
            dispatcher.dom_listener = dom_listener
            page.on("domcontentloaded", dom_listener)

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True
            try:
                dispatcher.callbacks.remove(my_callback)
            except ValueError:
                pass
            # Last subscriber out turns off the navigation listener.
            # (expose_function can't be unbound, but the dispatcher just
            # iterates an empty list — harmless.)
            if not dispatcher.callbacks and dispatcher.dom_listener is not None:
                try:
                    page.remove_listener("domcontentloaded", dispatcher.dom_listener)
                except Exception:
                    pass
                dispatcher.dom_listener = None

        return cleanup

    async def check(self, page: "Page") -> DetectionResult:
        """Check if element conditions are met."""
        try:
            for selector in self.present:
                element = await page.query_selector(selector)
                if element is None:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Required element '{selector}' not present",
                    )

            for selector in self.missing:
                element = await page.query_selector(selector)
                if element is not None:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Element '{selector}' should be missing but exists",
                    )

            for selector in self.visible:
                element = await page.query_selector(selector)
                if element is None:
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Visible element '{selector}' not found",
                    )
                if not await element.is_visible():
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Element '{selector}' exists but is not visible",
                    )

            for selector in self.hidden:
                element = await page.query_selector(selector)
                if element is not None and await element.is_visible():
                    return DetectionResult(
                        matched=False,
                        detection_type=self.detection_type,
                        reason=f"Element '{selector}' should be hidden but is visible",
                    )

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
        return cls(
            present=data.get("present", []),
            missing=data.get("missing", []),
            visible=data.get("visible", []),
            hidden=data.get("hidden", []),
        )
