"""Element-based detection (DOM element presence/absence/visibility)."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page


def _observer_setup_js(var: str) -> str:
    """JS injected once per document: a MutationObserver that stamps a hidden
    window property with Date.now() on any relevant DOM change. Python reads
    that stamp by polling — a plain property read, with no injected binding or
    callback function for the page to detect.

    `var` is a per-session random name, defined non-enumerable, so site JS can
    neither list it (Object.keys / for-in / JSON.stringify skip it) nor guess
    it to probe for it. The attributeFilter keeps the observer focused on the
    attributes that actually change element presence/visibility.
    """
    return (
        "(() => {"
        f"if (window.{var} !== undefined) return;"
        f"Object.defineProperty(window, '{var}', "
        "{value: 0, writable: true, enumerable: false, configurable: true});"
        f"const mark = () => {{ window.{var} = Date.now(); }};"
        "try {"
        "const mo = new MutationObserver(mark);"
        # Observe the document node (always present, even at document_start
        # when add_init_script re-injects after navigation — body/documentElement
        # may still be null then). subtree:true covers the whole tree.
        "mo.observe(document, "
        "{childList:true, subtree:true, attributes:true, "
        "attributeFilter:['class','style','hidden','disabled']});"
        "} catch (e) {}"
        "})();"
    )


def _observer_read_js(var: str) -> str:
    """JS that returns the latest mutation stamp (0 if none yet)."""
    return f"() => window.{var} || 0"


@dataclass
class ElementDetection(BaseDetection):
    """Detection based on DOM element presence, absence, or visibility.

    check() queries the page's DOM locally for the configured selectors. While
    watching (register_listeners), a MutationObserver stamps a hidden window var
    on every relevant DOM change and Python polls it, firing the callback (which
    re-runs check()) whenever the stamp advances, plus once per navigation so
    freshly-rendered DOM is evaluated. Each register_listeners is self-contained
    — its own observer, var, and poll loop — so any number of detections can
    watch one page independently.

    Example:
        detection = ElementDetection(
            present=["input[type=password]", "#login-form"],
            missing=[".user-menu", ".logout-button"],
            visible=[".consent-modal"],
            hidden=[".loading-spinner"],
        )
    """

    detection_type: str = field(default="element", init=False)

    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    visible: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)

    # How often the loop polls the JS mutation stamp. Cheap (reads a number);
    # doubles as the debounce window, so it's also the trigger latency.
    _poll_interval: float = field(default=0.1, init=False, repr=False)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        cleanup_called = False

        # Per-session, non-enumerable property name (see _observer_setup_js).
        observer_var = f"__bh_{secrets.token_hex(8)}"
        setup_js = _observer_setup_js(observer_var)
        read_js = _observer_read_js(observer_var)

        async def fire() -> None:
            if not cleanup_called:
                with suppress(Exception):
                    await callback(self)

        # Navigation is a check point: fire so a freshly-loaded (or SPA-routed)
        # page that already satisfies the condition is evaluated.
        def on_navigate(*_args: Any) -> None:
            loop.create_task(fire())

        page.on("framenavigated", on_navigate)

        async def watch() -> None:
            # Re-installs on every future document (init script) and on the one
            # already loaded (evaluate).
            with suppress(Exception):
                await page.add_init_script(setup_js)
            with suppress(Exception):
                await page.evaluate(setup_js)

            prev = 0
            while not stop_event.is_set():
                try:
                    ms = await page.evaluate(read_js)
                except Exception:
                    ms = prev
                if ms != prev:
                    prev = ms
                    # ms == 0 means a fresh/navigated document with no mutation
                    # yet — the reset itself isn't a change to report.
                    if ms:
                        await fire()

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass

        task = loop.create_task(watch())

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True
            stop_event.set()
            task.cancel()
            with suppress(Exception):
                page.remove_listener("framenavigated", on_navigate)

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
