"""Main Handoff class — single-method API for human-in-the-loop fallback."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
import warnings
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import load_file, load_json, load_yaml
from .detection.base import BaseDetection, DetectionResult
from .notifiers import (
    ConsoleNotifier,
    LinkItem,
    MessageItem,
    Notifier,
    TextItem,
    notifier_from_dict,
)
from .scenario import Scenario
from .server import ServerConfig, StreamingServer

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}

# JS run inside the handed-off Page to read the page's position and size on
# the substrate's display. The substrate streams the whole desktop (window
# chrome + display background); these six numbers let the proxy template crop
# the iframe down to just the page area.
#
# Note: window.screenY is the top of the *window*, not the page. Chrome
# (tabs + address bar) lives in `outerHeight - innerHeight`, so the actual
# page top on the display is `screenY + (outer - inner)`. Same logic for X
# in case the window has symmetric left/right borders.
_CROP_METRICS_JS = """() => ({
    screen_w: window.screen.width,
    screen_h: window.screen.height,
    page_x:   window.screenX + Math.max(0, (window.outerWidth  - window.innerWidth ) / 2),
    page_y:   window.screenY + Math.max(0, (window.outerHeight - window.innerHeight)),
    page_w:   window.innerWidth,
    page_h:   window.innerHeight,
})"""


async def _maximize_substrate_window(page: "Page") -> None:
    """Maximize the substrate browser window via CDP.

    Load-bearing for the crop math: when the window sits asymmetrically on
    the substrate display, CSS sub-pixel rounding on the iframe boundary
    produces 5-15px leakage strips that depend on which side the window is
    near (verified end-to-end against a real substrate). Maximizing forces
    screenX/Y to 0 and innerW to screen_w, which makes horizontal overflow
    exactly zero — the rendering then has nothing to round against on those
    sides, and the only overflow is the top chrome strip, which the existing
    math handles cleanly.

    Wrapped in try/except so a substrate that ignores
    Browser.setWindowBounds (headless mode, custom builds) doesn't break
    the handoff — we just get degraded crop quality, not failure.
    """
    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception as e:
        logger.info("could not open CDP session for maximize: %s", e)
        return
    try:
        wt = await cdp.send("Browser.getWindowForTarget")
        window_id = wt["windowId"]
        # Toggle through 'normal' first to force re-layout if the window is
        # already reported as maximized but bounds don't quite match the
        # display (observed on some Chromium builds — the second call to
        # 'maximized' is a no-op without this).
        await cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": "normal"},
        })
        await asyncio.sleep(0.2)
        await cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": "maximized"},
        })
        # Let the substrate re-layout and the remote stream catch up to the
        # new dimensions before we measure.
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.info("substrate window maximize failed: %s", e)


async def _capture_crop_metrics(
    page: "Page",
    *,
    attempts: int = 3,
    backoff: float = 0.1,
) -> dict[str, int] | None:
    """Query the page for its rect on the substrate's display.

    Maximizes the substrate browser window first (see
    `_maximize_substrate_window` — load-bearing for clean crop math), then
    reads the page rect via window.screen + window.screenX/Y +
    (outerH - innerH) chrome offset.

    Returns six ints when the page reports honest, non-degenerate values.
    Returns None when:
      - the evaluate raises (page detached, frame gone)
      - the page is mid-load and reports zero dims, even after retries
      - the substrate is headless and mocks screen dims to zero

    Retries with `backoff` between attempts so transient zeros (page just
    starting to navigate) get a chance to settle before we give up. When we
    do return None, the proxy template falls back to a non-cropped iframe —
    visually less polished but functionally fine.
    """
    await _maximize_substrate_window(page)

    for attempt in range(attempts):
        try:
            metrics = await page.evaluate(_CROP_METRICS_JS)
        except Exception as e:
            logger.info("crop metrics evaluate raised: %s", e)
            return None
        if metrics and metrics.get("screen_w") and metrics.get("page_w"):
            return {
                "screen_w": int(metrics["screen_w"]),
                "screen_h": int(metrics["screen_h"]),
                "page_x":   int(metrics["page_x"]),
                "page_y":   int(metrics["page_y"]),
                "page_w":   int(metrics["page_w"]),
                "page_h":   int(metrics["page_h"]),
            }
        if attempt < attempts - 1:
            await asyncio.sleep(backoff)
    logger.info(
        "crop metrics degenerate after %d attempts; falling back to no crop",
        attempts,
    )
    return None


def _detection_tree_has_llm(detection: BaseDetection) -> bool:
    """Recursive walk: does this detection (or any nested child) use LLM?

    Handles the three shapes any current detection can have:
      - leaf detection: isinstance check decides directly.
      - single-inner combinator (NotDetection): inspect `.condition`.
      - multi-inner combinator (AllDetection/AnyDetection): inspect
        `.conditions`.

    Used in two places:
      - Handoff.run() rejects a scenario whose trigger tree contains an
        LLMDetection. Trigger-mode LLM is broken (no operator yet, no
        reason context, MutationObserver-driven page noise) and the
        explicit reject names the scenario so users notice.
      - Handoff.wait_for_completion() skips its initial check when `on`
        is LLM-shaped — vision calls before the wrapper is even loaded
        are wasted, and the wrapper-presence gate inside the gated
        callback wouldn't pass anyway.
    """
    # Avoid an import cycle: llm module imports from detection.base which
    # is imported here. Lazy-import is fine; the function is hot-path
    # adjacent (called once per run/wait, not per check).
    from .detection.llm import LLMDetection

    if isinstance(detection, LLMDetection):
        return True
    if (inner := getattr(detection, "condition", None)) is not None:
        return _detection_tree_has_llm(inner)
    if (inners := getattr(detection, "conditions", None)) is not None:
        return any(_detection_tree_has_llm(c) for c in inners)
    return False


@dataclass
class HandoffResult:
    """Outcome of a Handoff.run() call.

    Three terminal states:
      - was_blocked=False                  → no trigger fired within timeout
      - was_blocked=True, timed_out=False  → human completed the task
      - was_blocked=True, timed_out=True   → human exceeded session_timeout
    """

    was_blocked: bool
    """Whether a trigger fired and a human handoff was performed."""

    timed_out: bool = False
    """Only meaningful if was_blocked: human exceeded session_timeout."""

    scenario_name: str | None = None
    """Name of the scenario that fired."""

    trigger_reason: str | None = None
    """Why the trigger matched (e.g. URL pattern, element appeared)."""

    completion_reason: str | None = None
    """Why the completion matched. None if not blocked or if timed out."""

    duration: float = 0.0
    """Seconds spent waiting for the human (0 if not blocked)."""


@dataclass
class Handoff:
    """Main handoff orchestrator.

    A Handoff bundles the transport config (streaming server, notifiers,
    viewport) and is reusable across many pages/runs. *What* to watch for is
    supplied per-call, so the same Handoff can serve any number of scenarios.

    Two entry points:

      - run(page, scenarios=...) — watch a page for trigger conditions, and
        when one fires, stream the page to a human and wait for the matching
        completion. Use this when you want the library to decide *when* a human
        is needed.

      - wait_for_completion(page, on=...) — stream the page to a human *now*
        and wait until `on` matches. Use this when you've already decided a
        human is needed (e.g. an agent framework detected the condition
        itself), so there's no trigger to watch.

    Pass scenarios per-call to `run(scenarios=...)`. The `scenarios`
    constructor argument is deprecated (and emits a DeprecationWarning): it
    couples *what to watch for* to the reusable transport object. Set them on
    `run()` instead.

    Streaming server lifecycle: a single server (on `server.port`) is shared
    by every handoff this instance performs. It starts lazily on the first
    handoff and stops when the last one finishes — concurrent handoffs run as
    separate sessions on the same port (distinguished by session id) and never
    collide, so you can drive many pages from one Handoff at once.

    Example:
        handoff = Handoff(notifiers=[DiscordNotifier(...)])  # reusable

        result = await handoff.run(
            page,
            scenarios=[
                Scenario(
                    name="login_required",
                    trigger=Detection.element(present=['input[type="email"]']),
                    complete=Detection.url(path_contains=["/dashboard"]),
                ),
            ],
        )
        if result.was_blocked and not result.timed_out:
            print(f"Human completed: {result.scenario_name}")
        await bot_logic(page)
    """

    scenarios: list[Scenario] = field(default_factory=list)
    server: ServerConfig = field(default_factory=ServerConfig)
    notifiers: list[Notifier] = field(default_factory=list)
    viewport_size: dict[str, int] = field(default_factory=lambda: DEFAULT_VIEWPORT.copy())

    # Runtime state for the shared streaming server. Not constructor args —
    # see _acquire_server / _release_server for the lazy-start, ref-counted
    # lifecycle. _session_count tracks live handoffs; the server stops when it
    # returns to zero. _server_lock serializes start/stop so concurrent
    # handoffs neither double-bind the port nor overlap a start with a stop.
    _server: StreamingServer | None = field(default=None, init=False, repr=False, compare=False)
    _server_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False, compare=False)
    _session_count: int = field(default=0, init=False, repr=False, compare=False)
    _server_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.scenarios:
            warnings.warn(
                "Passing `scenarios` to Handoff() is deprecated; pass them "
                "per-call to run(scenarios=...) instead. The constructor "
                "argument will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )

    @classmethod
    def from_file(cls, path: str | Path) -> "Handoff":
        cls._warn_from_deprecated("from_file")
        return cls._build_from_dict(load_file(path))

    @classmethod
    def from_json(cls, json_string: str) -> "Handoff":
        cls._warn_from_deprecated("from_json")
        return cls._build_from_dict(load_json(json_string))

    @classmethod
    def from_yaml(cls, yaml_string: str) -> "Handoff":
        cls._warn_from_deprecated("from_yaml")
        return cls._build_from_dict(load_yaml(yaml_string))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Handoff":
        cls._warn_from_deprecated("from_dict")
        return cls._build_from_dict(data)

    @staticmethod
    def _warn_from_deprecated(name: str) -> None:
        warnings.warn(
            f"Handoff.{name}() is deprecated and will be removed in the next "
            "major release. Build the Handoff yourself from its parts instead: "
            "parse the config with Scenario.from_dict / ServerConfig.from_dict "
            "/ notifier_from_dict, then `Handoff(server=..., notifiers=...)` and "
            "`run(scenarios=...)`.",
            DeprecationWarning,
            stacklevel=3,
        )

    @classmethod
    def _build_from_dict(cls, data: dict[str, Any]) -> "Handoff":
        """Assemble a Handoff from a config dict (no deprecation warning).

        Shared by the deprecated from_* loaders. Scenarios are assigned after
        construction so the deprecated `scenarios` constructor argument — and
        its warning — is not involved.
        """
        scenarios = [Scenario.from_dict(s) for s in data.get("scenarios", [])]
        server_data = data.get("server", {})
        server = ServerConfig.from_dict(server_data) if server_data else ServerConfig()
        notifiers = [notifier_from_dict(n) for n in data.get("notifiers", [])]
        inst = cls(server=server, notifiers=notifiers)
        inst.scenarios = scenarios
        return inst

    @property
    def live_session_count(self) -> int:
        """Number of handoffs currently streaming on the shared server."""
        return self._session_count

    @property
    def is_serving(self) -> bool:
        """Whether the shared streaming server is currently running."""
        return self._server is not None

    async def run(
        self,
        page: "Page",
        *,
        scenarios: list[Scenario] | None = None,
        timeout: float = 30.0,
        stream_url: str | None = None,
    ) -> "HandoffResult":
        """Wait until handoff completes, or no handoff is needed.

        Registers framenavigated/element-mutation listeners on every scenario's
        trigger, then waits for either:
          - A trigger to fire → run the human handoff and wait for the
            scenario's completion condition (bounded by
            self.server.session_timeout).
          - `timeout` seconds to elapse with no trigger → return was_blocked=False.

        The browser context is auto-detected from page.context — there's no
        case where you'd want a different one for the same page.

        Args:
            page: Playwright page to monitor.
            scenarios: Trigger-completion pairs to watch. Falls back to the
                scenarios set on the instance (e.g. via from_file) when omitted.
                Raises ValueError if neither is provided.
            timeout: Max seconds to wait for any trigger to fire before
                concluding no handoff is needed. Default: 30.0.
                Does NOT bound the human-completion phase — that uses
                self.server.session_timeout (default 600s, set on
                ServerConfig).
            stream_url: Optional substrate-served viewer URL. When set, the
                matched scenario's handoff runs in passthrough mode:
                browser-handoff skips its own CDP screencast and the operator
                gets a wrapper page that iframes this URL. Forwarded as-is
                to wait_for_completion on trigger match.

        Returns:
            HandoffResult describing what happened. Never raises on
            human-completion timeout — check result.timed_out instead.
        """
        scenarios = scenarios if scenarios is not None else self.scenarios
        if not scenarios:
            raise ValueError(
                "run() requires at least one scenario: pass scenarios=[...] "
                "or set them on Handoff(...). To stream without a trigger, use "
                "wait_for_completion()."
            )

        # LLMDetection in a trigger tree is a misuse: there's no operator
        # yet to ground "did they finish", no reason for the prompt, and
        # the only signal would be page DOM noise — which produces a
        # vision-call-per-mutation hot loop on real sites. Reject with a
        # clear message naming the scenario so the user can fix the
        # scenario, not chase a vague runtime symptom later. Walk the
        # trigger tree to catch combinator nesting.
        for scenario in scenarios:
            if _detection_tree_has_llm(scenario.trigger):
                raise TypeError(
                    f"Scenario {scenario.name!r} uses LLMDetection in its "
                    "trigger (possibly nested inside a combinator). "
                    "LLMDetection is only valid as a completion check via "
                    "Handoff.wait_for_completion (or as Scenario.complete). "
                    "Use URL/Element/Content detections for triggers."
                )

        trigger_event = asyncio.Event()
        matched_scenario: Scenario | None = None
        matched_result: DetectionResult | None = None
        detection_to_scenario: dict[int, Scenario] = {
            id(s.trigger): s for s in scenarios
        }

        async def on_trigger(detection: BaseDetection) -> None:
            nonlocal matched_scenario, matched_result
            if trigger_event.is_set():
                return
            r = await detection.check(page)
            if r.matched:
                matched_scenario = detection_to_scenario[id(detection)]
                matched_result = r
                trigger_event.set()

        cleanups: list[Any] = [
            s.trigger.register_listeners(page, on_trigger) for s in scenarios
        ]

        try:
            # Initial check — page may already be in a triggered state when
            # called (e.g. caller awaited goto() then immediately ran us).
            for scenario in scenarios:
                r = await scenario.trigger.check(page)
                if r.matched:
                    matched_scenario = scenario
                    matched_result = r
                    trigger_event.set()
                    break

            if not trigger_event.is_set():
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(trigger_event.wait(), timeout=timeout)

            if matched_scenario is None or matched_result is None:
                logger.info(
                    "handoff.run: no trigger matched within %.1fs (page url=%s)",
                    timeout, page.url,
                )
                return HandoffResult(was_blocked=False)

            logger.info(
                "handoff.run: trigger matched (scenario='%s'): %s",
                matched_scenario.name, matched_result.reason,
            )
            return await self.wait_for_completion(
                page,
                matched_scenario.complete,
                reason=matched_result.reason,
                name=matched_scenario.name,
                stream_url=stream_url,
            )
        finally:
            for cleanup in cleanups:
                with suppress(Exception):
                    cleanup()

    async def wait_for_completion(
        self,
        page: "Page",
        on: BaseDetection,
        *,
        reason: str = "Human intervention required",
        name: str = "handoff",
        stream_url: str | None = None,
    ) -> "HandoffResult":
        """Stream the page to a human *now* and wait until `on` matches.

        Unlike run(), this skips trigger detection entirely — use it when
        you've already decided a human is needed (e.g. an agent framework
        detected the condition and called you), so watching for a trigger
        would be redundant. run() funnels here once its trigger fires.

        Runs as one session on the instance's shared streaming server (see
        _acquire_server): the server starts on the first concurrent handoff
        and stops when the last finishes, so handoffs never collide on the
        port — each is just a distinct session id.

        Args:
            page: Playwright page to stream. Streaming starts immediately.
            on: Completion detection that signals the human is done. The
                handoff returns the moment it matches (or when the page
                already satisfies it on entry).
            reason: Human-facing explanation shown in the notification and the
                operator UI. Defaults to a generic message.
            name: Label recorded on the result (HandoffResult.scenario_name).
            stream_url: Optional substrate viewer URL. When set, this handoff
                runs in passthrough mode: browser-handoff skips its own CDP
                screencast and the operator gets a wrapper page that iframes
                this URL. browser-handoff still owns detection, notification,
                and lifecycle. The wrapper page crops the iframe to just the
                page content via a one-shot JS query at handoff start.

        Returns:
            HandoffResult with was_blocked=True. Check timed_out for whether
            the human finished within self.server.session_timeout. Never
            raises on timeout.
        """
        context = page.context
        start_time = time.time()
        session_id = str(uuid.uuid4())[:8]
        listener_cleanups: list[Any] = []
        completion_event = asyncio.Event()
        completion_reason: str | None = None

        # Captured here so the gated callback below (defined before the
        # session exists) can read the session through this closure cell
        # once register_session has returned. Without this hop the
        # callback would need session passed in explicitly, which forces
        # a re-bind on every fire.
        session_ref: dict[str, Any] = {"session": None}

        async def on_completion_detected(detection: BaseDetection) -> None:
            nonlocal completion_reason
            if completion_event.is_set():
                return
            session = session_ref["session"]
            # Wrapper-presence gate: the watcher inside LLMDetection
            # fires on its own schedule (idle-settle + safety-net), so
            # by the time it calls back the operator may have closed
            # the tab. Skipping here saves the LLM call. Other
            # detections (URL/Element/Content) are cheap, but routing
            # them through the same gate keeps the orchestration model
            # consistent — completion shouldn't be reported against a
            # session with no operator present.
            if session is not None and session.presence.state != "present":
                return
            # `reason` is what LLMDetection needs to ground its prompt;
            # other detections accept and ignore via **context. The
            # session.reason is the same string the operator sees in the
            # wrapper header, so it's the right framing for the model.
            ctx_reason = session.reason if session is not None else reason
            result = await detection.check(page, reason=ctx_reason)
            if result.matched:
                completion_reason = result.reason
                completion_event.set()

        server = await self._acquire_server()
        try:
            viewport_size = self.viewport_size
            try:
                actual_viewport = page.viewport_size
                if actual_viewport:
                    viewport_size = actual_viewport
                else:
                    dimensions = await page.evaluate(
                        "() => ({ width: window.innerWidth, height: window.innerHeight })"
                    )
                    if dimensions and dimensions.get("width") and dimensions.get("height"):
                        viewport_size = dimensions
            except Exception as e:
                logger.info(f"Could not get viewport: {e}, using default: {viewport_size}")

            # Capture page-rect-on-display metrics for the proxy template's
            # iframe crop. Only matters in passthrough mode; in normal mode
            # we'd just be doing work for no reason.
            crop_metrics: dict[str, int] | None = None
            if stream_url is not None:
                crop_metrics = await _capture_crop_metrics(page)

            session = await server.register_session(
                session_id=session_id,
                page=page,
                context=context,
                reason=reason,
                scenario_name=name,
                viewport_size=viewport_size,
                stream_url=stream_url,
                crop_metrics=crop_metrics,
            )
            session_ref["session"] = session

            operator_url = server.get_operator_url(session_id)

            logger.info("=" * 70)
            logger.info("HANDOFF: Human intervention required")
            logger.info("Reason: %s", reason)
            logger.info("Scenario: %s", name)
            logger.info("Operator URL: %s", operator_url)
            logger.info("=" * 70)

            await self._send_notifications(reason, operator_url)

            # Already complete? (e.g. page raced past completion before we
            # finished setting up listeners.) Run as a cheap check for the
            # non-LLM paths (URL/Element/Content). LLM completion is not
            # initial-eligible (would burn a vision call before the
            # wrapper is even loaded), so skip the initial probe when `on`
            # is LLM-shaped.
            if not _detection_tree_has_llm(on):
                initial = await on.check(page, reason=reason)
                if initial.matched:
                    completion_reason = initial.reason
                    completion_event.set()

            # Lazy install: defer detection.register_listeners until an
            # operator has actually opened the wrapper. Closes the
            # substrate-URL leak — anyone reaching the substrate viewer
            # URL directly can no longer drive vision calls, because the
            # in-page watcher only installs after wrapper auth via
            # access_token. session_timeout bounds the wait so "operator
            # never showed up" still terminates the handoff cleanly.
            timed_out = False
            try:
                if not completion_event.is_set():
                    await asyncio.wait_for(
                        session.presence.wait_until_connected(),
                        timeout=self.server.session_timeout,
                    )

                if not completion_event.is_set():
                    listener_cleanups.append(
                        on.register_listeners(page, on_completion_detected)
                    )

                await asyncio.wait_for(
                    completion_event.wait(),
                    timeout=self.server.session_timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning(
                    "Handoff session_timeout: human did not finish "
                    "within %.0fs", self.server.session_timeout,
                )
            except asyncio.CancelledError:
                # Caller (e.g. browser-use's per-step timeout, ctrl-c)
                # cancelled the await. Surface a task_cancelled event so
                # the operator's wrapper distinguishes "the agent gave up"
                # from "you ran out of time" (task_expired) — both end the
                # session but for different reasons, and the operator
                # debugging the situation needs accurate framing. Without
                # the notify the passthrough iframe would stay interactive
                # against a substrate bh no longer owns. Re-raise so the
                # caller's cancellation semantics are preserved.
                with suppress(Exception):
                    await server.notify_task_cancelled(session_id)
                raise

            await server.stop_screencast(session_id)

            if timed_out:
                await server.notify_task_expired(session_id)
            elif completion_reason:
                await server.notify_task_completed(session_id, completion_reason)

            return HandoffResult(
                was_blocked=True,
                timed_out=timed_out,
                scenario_name=name,
                trigger_reason=reason,
                completion_reason=None if timed_out else completion_reason,
                duration=time.time() - start_time,
            )

        finally:
            for cleanup in listener_cleanups:
                with suppress(Exception):
                    cleanup()
            with suppress(Exception):
                await server.unregister_session(session_id)
            await self._release_server()

    async def _acquire_server(self) -> StreamingServer:
        """Return the shared streaming server, starting it on first use.

        Reference-counted: every caller that acquires must pair with a
        _release_server() in its finally. The start (bind + readiness wait)
        happens under the lock, so concurrent first handoffs don't both try to
        bind the port — the second waits, sees the server already up, and just
        joins it as another session.
        """
        async with self._server_lock:
            if self._server is None:
                server = StreamingServer(config=self.server)
                self._server_task = asyncio.create_task(server.start())
                await self._wait_for_port(self.server.host, self.server.port)
                self._server = server
            self._session_count += 1
            return self._server

    async def _release_server(self) -> None:
        """Drop one handoff's hold on the shared server.

        When the last session leaves (count hits zero) the server is stopped
        under the lock — so a handoff arriving in the same instant blocks until
        the stopping server has fully released the port before a new one binds.
        Start and stop therefore never overlap.
        """
        async with self._server_lock:
            self._session_count -= 1
            if self._session_count > 0:
                return

            server, task = self._server, self._server_task
            self._server = None
            self._server_task = None

            if server:
                await server.stop()
            if task and not task.done():
                # server.stop() already signaled should_exit and closed the
                # client connections. Let uvicorn unwind on its own; only
                # cancel as a last resort if it hangs.
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.TimeoutError:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                except asyncio.CancelledError:
                    pass

    @staticmethod
    async def _wait_for_port(
        host: str, port: int, timeout: float = 5.0, interval: float = 0.05
    ) -> None:
        """Poll until uvicorn is accepting connections on host:port.

        Replaces a magic asyncio.sleep — works regardless of how slow the
        machine is to bind, and returns the moment the port is ready.

        Each connect attempt has its own short timeout. Without it, a
        single attempt that stalls mid-handshake (uvicorn between bind and
        accept; WSL2 loopback quirks; firewall holding the SYN) would hang
        forever — the outer deadline is only checked between iterations
        and never fires.
        """
        # 0.0.0.0 / :: are bind addresses, not connect addresses.
        connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else (
            "::1" if host == "::" else host
        )
        # Tight per-attempt cap so a stuck connect doesn't starve the loop.
        # interval is the retry pause AFTER a failure; per_attempt is the
        # ceiling on a single try.
        per_attempt = min(1.0, max(interval * 4, 0.2))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(connect_host, port),
                    timeout=per_attempt,
                )
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(interval)
        logger.warning(
            "Streaming server did not accept connections on %s:%d within %.1fs",
            connect_host, port, timeout,
        )

    async def _send_notifications(self, reason: str, operator_url: str) -> None:
        # No explicit notifiers → fall back to a rich console panel so the
        # operator still gets a clearly-formatted stream URL. When the
        # caller configures any notifier(s) we stay out of the way — they
        # asked for those specific channels, double-pushing to stdout
        # would just be noise.
        notifiers = self.notifiers or [ConsoleNotifier()]

        title = "Human Intervention Required"
        # Structured items let each notifier render natively (Rich link
        # markup, Discord embed url field, Slack mrkdwn hyperlinks, HTML
        # <a> in email) without parsing a flat template back out.
        items: list[MessageItem] = [
            TextItem(
                "Human intervention is required to complete a browser automation task."
            ),
            TextItem(f"Reason: {reason}"),
            LinkItem(prefix="Stream URL: ", url=operator_url),
            TextItem("Please open the stream URL to assist with the task."),
        ]

        async def send_notification(notifier: Notifier) -> None:
            try:
                await notifier.send(title=title, message=items, urgency="critical")
            except Exception as e:
                logger.error(
                    "Failed to send notification via %s: %s",
                    type(notifier).__name__, e,
                )

        await asyncio.gather(
            *[send_notification(n) for n in notifiers],
            return_exceptions=True,
        )
