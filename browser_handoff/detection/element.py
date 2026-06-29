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
    """Inject a MutationObserver that stamps `window[var]` with Date.now().

    `var` is non-enumerable and per-session random, so site JS can't list
    or probe it. Python reads the stamp via polling — no injected binding
    or callback for the page to fingerprint.
    """
    return (
        "(() => {"
        f"if (window.{var} !== undefined) return;"
        f"Object.defineProperty(window, '{var}', "
        "{value: 0, writable: true, enumerable: false, configurable: true});"
        f"const mark = () => {{ window.{var} = Date.now(); }};"
        "try {"
        "const mo = new MutationObserver(mark);"
        # Observe document, not body — body may still be null when
        # add_init_script re-runs at document_start after navigation.
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
    """A (detection, callback) pair attached to a `_PageWatcher`.

    `cancelled` is flipped synchronously by cleanup so callbacks already
    in flight or scheduled before the async unsubscribe lands are
    skipped — no callback fires after cleanup() returns.

    `eq=False` keeps identity equality so each registration is a
    distinct entry in the watcher's `set[_Subscriber]`.
    """

    detection: "BaseDetection"
    callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]]
    cancelled: bool = False


class _PageWatcher:
    """One MutationObserver + one poll loop per page, shared by every
    `ElementDetection` subscription on that page.

    Installs on the first subscriber, tears down when the last leaves.
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
        # Serialises install/teardown so a subscribe arriving mid-shutdown
        # can't race into an inconsistent state.
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._installed = False
        self._closed = False
        # Stored so remove_listener can match by identity at teardown.
        self._on_navigate_handler: Callable[..., None] | None = None

    def _on_navigate(self, *_: Any) -> None:
        """Re-inject the observer on the new document and fire subscribers.

        Re-injection lives here (not via `add_init_script`) so removing
        this listener at teardown also stops future re-injection.
        """
        if not self._closed:
            self._loop.create_task(self._reinject_and_fire())

    async def _reinject_and_fire(self) -> None:
        with suppress(Exception):
            await self._page.evaluate(_observer_setup_js(self._var))
        await self._fire_all()

    async def add(self, sub: _Subscriber) -> bool:
        """Register a subscriber; install the watcher on first use.

        Args:
            sub: Subscriber to attach.

        Returns:
            True on success. False if the watcher had already closed
            (caller should retry with a fresh watcher from the registry).
        """
        async with self._lock:
            if self._closed:
                return False
            self._subscribers.add(sub)
            if self._installed:
                return True
            # Attach the nav listener BEFORE seeding the current doc, so
            # a navigation racing the initial evaluate is still caught.
            self._on_navigate_handler = self._on_navigate
            self._page.on("framenavigated", self._on_navigate_handler)
            with suppress(Exception):
                await self._page.evaluate(_observer_setup_js(self._var))
            self._poll_task = self._loop.create_task(self._poll())
            self._installed = True
            return True

    async def remove(self, sub: _Subscriber) -> bool:
        """Drop a subscriber; tear down on the last leave.

        Args:
            sub: Subscriber to detach.

        Returns:
            True if the watcher was torn down (caller should evict from
            the registry); False otherwise.
        """
        async with self._lock:
            self._subscribers.discard(sub)
            if self._subscribers or self._closed:
                return False
            self._close_locked()
            return True

    async def shutdown(self) -> None:
        """Force teardown regardless of subscribers (called on page close)."""
        async with self._lock:
            if self._closed:
                return
            self._subscribers.clear()
            self._close_locked()

    def _close_locked(self) -> None:
        """Teardown body. Caller must hold `self._lock`."""
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
                # ms == 0 is a fresh/navigated document — the reset
                # itself isn't a change to report.
                if ms:
                    await self._fire_all()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _fire_all(self) -> None:
        # Snapshot — a callback may schedule an unsubscribe, and a
        # cancelled subscriber whose remove hasn't run yet must be skipped.
        for sub in list(self._subscribers):
            if sub.cancelled:
                continue
            with suppress(Exception):
                await sub.callback(sub.detection)


# Page → watcher registry, keyed by id(page) (Playwright Pages aren't
# reliably hashable across implementations, and we want identity anyway).
_watchers: dict[int, _PageWatcher] = {}


def _watcher_for(
    page: "Page", loop: asyncio.AbstractEventLoop, poll_interval: float
) -> _PageWatcher:
    """Get or create the per-page watcher; first caller wins the interval."""
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
    """Match on DOM selector presence, absence, or visibility.

    All four clauses are AND: every `present` must exist, every `missing`
    must not, every `visible` must be visible, every `hidden` must not be
    visible. Subscriptions on a page share one MutationObserver.

    Example:
        ElementDetection(
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

    # Mutation poll cadence; also the trigger latency. Only the first
    # subscriber on a page wins the interval — the shared watcher
    # inherits it from whoever brings it up.
    _poll_interval: float = field(default=0.1, init=False, repr=False)

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Subscribe to the page's shared mutation watcher.

        Args:
            page: Playwright page to observe.
            callback: Async function invoked with `self` on every
                mutation tick.

        Returns:
            A cleanup function that drops the subscription (and tears
            down the watcher if it was the last subscriber).
        """
        loop = asyncio.get_running_loop()
        sub = _Subscriber(detection=self, callback=callback)

        async def _do_add() -> _PageWatcher:
            # Look up at task-run time so a teardown completing between
            # call and run is visible. On a closed watcher (race),
            # evict the stale entry and retry with a fresh one.
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
            # Flip cancelled synchronously so any in-flight or pre-
            # scheduled _fire_all skips this subscriber before remove() runs.
            sub.cancelled = True

            async def _do_remove() -> None:
                # Wait for the add to land so we know which watcher we
                # joined — otherwise we'd race the install and leak.
                try:
                    watcher = await add_task
                except Exception:
                    return
                if await watcher.remove(sub):
                    # Only evict if this is still the registered watcher;
                    # a fresh registration may have replaced it.
                    if _watchers.get(id(page)) is watcher:
                        _watchers.pop(id(page), None)

            loop.create_task(_do_remove())

        return cleanup

    async def check(self, page: "Page", **context: Any) -> DetectionResult:
        """Return a match only when every configured selector clause holds.

        Args:
            page: Playwright page to inspect.
            **context: Unused.
        """
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

            # Name the matching selectors in `reason` so logs surface
            # which clauses fired.
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
