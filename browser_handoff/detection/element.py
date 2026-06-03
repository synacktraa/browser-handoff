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


# ---- shared per-page watcher --------------------------------------------


@dataclass(eq=False)
class _Subscriber:
    """A single (detection, callback) pair attached to a `_PageWatcher`.

    `cancelled` is set synchronously by `register_listeners`' cleanup so any
    callback already in flight or scheduled before the async unsubscribe runs
    is dropped on the floor — preserving the invariant that no callback fires
    after cleanup() has returned.

    `eq=False` keeps identity-based equality and hashing — each registration
    is a distinct subscriber even if fields collide, and the watcher's
    `set[_Subscriber]` needs hashability.
    """

    detection: "BaseDetection"
    callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]]
    cancelled: bool = False


class _PageWatcher:
    """One MutationObserver + one poll loop per Playwright page, shared by
    every `ElementDetection` subscription on that page.

    Installs on the first subscriber and tears down when the last leaves —
    so a page with N element detections runs a single observer, a single
    init script, and a single 100ms poll loop instead of N of each.
    """

    def __init__(
        self,
        page: "Page",
        loop: asyncio.AbstractEventLoop,
        poll_interval: float,
    ) -> None:
        self._page = page
        self._loop = loop
        self._poll_interval = poll_interval
        self._var = f"__bh_{secrets.token_hex(8)}"
        self._subscribers: set[_Subscriber] = set()
        # Serialises install / teardown decisions so a subscribe arriving
        # mid-teardown can't race the shutdown into an inconsistent state.
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._installed = False
        self._closed = False
        # Held so we can remove the listener during teardown — Playwright's
        # remove_listener matches by identity, not by name.
        self._on_navigate_handler: Callable[..., None] | None = None

    def _on_navigate(self, *_: Any) -> None:
        """Re-install the observer on the new document and fire subscribers.

        Re-injection lives here (not in `page.add_init_script`) so teardown of
        this listener also stops future re-injection — after the last cleanup,
        nothing re-runs the setup script, and the page is left clean.
        """
        if not self._closed:
            self._loop.create_task(self._reinject_and_fire())

    async def _reinject_and_fire(self) -> None:
        with suppress(Exception):
            await self._page.evaluate(_observer_setup_js(self._var))
        await self._fire_all()

    async def add(self, sub: _Subscriber) -> bool:
        """Register a subscriber, installing the watcher on first use.

        Returns False if the watcher was already closed (the caller raced a
        concurrent teardown); the caller retries with a fresh watcher in that
        case so the subscriber isn't orphaned.
        """
        async with self._lock:
            if self._closed:
                return False
            self._subscribers.add(sub)
            if self._installed:
                return True
            # Attach the navigation listener BEFORE seeding the current doc,
            # so a navigation racing the initial evaluate is still caught and
            # re-installs the observer on whatever document wins.
            self._on_navigate_handler = self._on_navigate
            self._page.on("framenavigated", self._on_navigate_handler)
            with suppress(Exception):
                await self._page.evaluate(_observer_setup_js(self._var))
            self._poll_task = self._loop.create_task(self._poll())
            self._installed = True
            return True

    async def remove(self, sub: _Subscriber) -> bool:
        """Drop a subscriber; if it was the last, tear the watcher down.

        Returns True if the watcher was torn down (the caller should evict
        it from the registry), False otherwise.
        """
        async with self._lock:
            self._subscribers.discard(sub)
            if self._subscribers or self._closed:
                return False
            self._close_locked()
            return True

    async def shutdown(self) -> None:
        """Force teardown regardless of subscribers (called when the page closes)."""
        async with self._lock:
            if self._closed:
                return
            self._subscribers.clear()
            self._close_locked()

    def _close_locked(self) -> None:
        """Teardown body. Must be called with `self._lock` held."""
        self._closed = True
        self._stop.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
        if self._on_navigate_handler is not None:
            with suppress(Exception):
                self._page.remove_listener("framenavigated", self._on_navigate_handler)
            self._on_navigate_handler = None

    async def _poll(self) -> None:
        read_js = _observer_read_js(self._var)
        prev = 0
        while not self._stop.is_set():
            try:
                ms = await self._page.evaluate(read_js)
            except Exception:
                ms = prev
            if ms != prev:
                prev = ms
                # ms == 0 means a fresh/navigated document with no mutation
                # yet — the reset itself isn't a change to report.
                if ms:
                    await self._fire_all()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _fire_all(self) -> None:
        # Snapshot first — a callback may schedule an unsubscribe, and we
        # also want cancelled subscribers (cleanup ran but remove hasn't yet)
        # to be skipped without racing the set's contents.
        for sub in list(self._subscribers):
            if sub.cancelled:
                continue
            with suppress(Exception):
                await sub.callback(sub.detection)


# Page → watcher registry. Keyed by id(page) because Playwright Page objects
# aren't reliably hashable across implementations and we want identity, not
# equality. Eviction happens on the last unsubscribe or on page close.
_watchers: dict[int, _PageWatcher] = {}


def _watcher_for(
    page: "Page", loop: asyncio.AbstractEventLoop, poll_interval: float
) -> _PageWatcher:
    """First call for a page wins the poll interval; later subscribers share it."""
    key = id(page)
    watcher = _watchers.get(key)
    if watcher is not None:
        return watcher
    watcher = _PageWatcher(page, loop, poll_interval)
    _watchers[key] = watcher

    def _on_close(*_: Any) -> None:
        existing = _watchers.pop(key, None)
        if existing is not None:
            loop.create_task(existing.shutdown())

    with suppress(Exception):
        page.on("close", _on_close)
    return watcher


@dataclass
class ElementDetection(BaseDetection):
    """Detection based on DOM element presence, absence, or visibility.

    check() queries the page's DOM locally for the configured selectors. While
    watching (register_listeners), all element subscriptions on a given page
    share a single MutationObserver: one observer stamps a hidden window var,
    one Python poll loop reads it, and every subscriber's callback fires when
    the stamp advances (plus once per navigation so freshly-rendered DOM is
    evaluated). The shared watcher installs on the first subscriber and tears
    down when the last cleanup() runs.

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

    # How often the shared page watcher polls the JS mutation stamp. Cheap
    # (reads a number); doubles as the debounce window, so it's also the
    # trigger latency. Lowering it on a detection only matters when that
    # detection is the FIRST subscriber on a page — the watcher inherits the
    # interval from whichever detection brings it up.
    _poll_interval: float = field(default=0.1, init=False, repr=False)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        sub = _Subscriber(detection=self, callback=callback)

        async def _do_add() -> _PageWatcher:
            # Look up at task-run time, not at register_listeners-call time,
            # so a teardown completing between call and run is visible. On a
            # closed watcher (race: cleanup of the previous subscriber drained
            # the dict during a separate scheduling tick), evict the stale
            # entry and retry — the next _watcher_for creates a fresh one.
            while True:
                watcher = _watcher_for(page, loop, self._poll_interval)
                if await watcher.add(sub):
                    return watcher
                if _watchers.get(id(page)) is watcher:
                    _watchers.pop(id(page), None)

        add_task = loop.create_task(_do_add())

        cleaned = False

        def cleanup() -> None:
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            # Flip cancelled synchronously so any in-flight or pre-scheduled
            # _fire_all skips this subscriber even before remove() runs.
            sub.cancelled = True

            async def _do_remove() -> None:
                # Wait for the add to land so we know which watcher we joined,
                # otherwise we'd race the install and leak a subscription.
                try:
                    watcher = await add_task
                except Exception:
                    return
                if await watcher.remove(sub):
                    # Only evict if this watcher is still the one in the
                    # registry — a fresh registration may have replaced it.
                    if _watchers.get(id(page)) is watcher:
                        _watchers.pop(id(page), None)

            loop.create_task(_do_remove())

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

            # Name the selectors that satisfied each configured clause so
            # the operator can see what specifically matched — not just a
            # generic "all conditions met".
            parts: list[str] = []
            if self.present:
                parts.append("present=" + repr(self.present))
            if self.missing:
                parts.append("missing=" + repr(self.missing))
            if self.visible:
                parts.append("visible=" + repr(self.visible))
            if self.hidden:
                parts.append("hidden=" + repr(self.hidden))
            reason = (
                "Elements matched: " + ", ".join(parts)
                if parts
                else "Elements matched (no conditions configured)"
            )

            return DetectionResult(
                matched=True,
                detection_type=self.detection_type,
                reason=reason,
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
