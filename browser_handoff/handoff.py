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
from typing import TYPE_CHECKING, Any, Literal

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

# Read the page's rect on the substrate's display so the passthrough
# template can crop the iframe to just the page area (the substrate streams the
# whole desktop). page_y accounts for browser chrome via
# `outerHeight - innerHeight`; page_x mirrors that in case of symmetric
# window borders.
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

    Load-bearing for crop math: an asymmetric window position produces
    CSS sub-pixel rounding leakage (5-15px strips) on the iframe edges.
    Maximizing forces screenX/Y to 0 and innerW to screen_w so the only
    overflow is the top chrome strip, which the math already handles.

    Best-effort — substrates that ignore Browser.setWindowBounds
    (headless, custom builds) degrade crop quality but don't fail.
    """
    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception as e:
        logger.info("could not open CDP session for maximize: %s", e)
        return
    try:
        wt = await cdp.send("Browser.getWindowForTarget")
        window_id = wt["windowId"]
        # Toggle through 'normal' first — some Chromium builds report
        # already-maximized state but with off-by-N bounds, and a
        # second 'maximized' call is a no-op without this nudge.
        await cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": "normal"},
        })
        await asyncio.sleep(0.2)
        await cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": "maximized"},
        })
        # Let the substrate re-layout settle before we measure.
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.info("substrate window maximize failed: %s", e)


async def _capture_crop_metrics(
    page: "Page",
    *,
    attempts: int = 3,
    backoff: float = 0.1,
) -> dict[str, int] | None:
    """Read the page's rect on the substrate's display.

    Maximizes the substrate window first (load-bearing for clean crop
    math), then reads `window.screen` + `screenX/Y` + chrome offset.

    Returns None when the evaluate raises, the page reports zero dims
    even after retries, or the substrate mocks screen dims (headless).
    On None, the passthrough template falls back to a non-cropped iframe.
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


def _resolve_timeout(
    per_call: float | None, default: float | None
) -> float | None:
    """Resolve a per-call timeout against the ServerConfig default.

    `None` per-call inherits the default; any other value (including
    `math.inf`) overrides. `None` on both layers means truly disabled.
    """
    return default if per_call is None else per_call


def _detection_tree_has_llm(detection: BaseDetection) -> bool:
    """True if `detection` or any nested child is an LLMDetection.

    Walks the three current detection shapes — leaf, single-inner
    combinator (`.condition`), and multi-inner combinator
    (`.conditions`). Used by `Handoff.guard` to reject LLM triggers and
    by `pause` to skip the initial check for LLM `on`.
    """
    # Lazy import: llm imports detection.base which imports this module.
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
    """Outcome of a Handoff.guard() / Handoff.pause() call.

    Three terminal states:
      - was_blocked=False                  → no trigger fired within trigger_timeout
      - was_blocked=True, timed_out=False  → human completed the task
      - was_blocked=True, timed_out=True   → one of the handoff timers fired
    """

    was_blocked: bool
    """Whether a trigger fired and a human handoff ran."""

    timed_out: bool = False
    """Only meaningful if `was_blocked`: either timer fired."""

    timeout_cause: Literal["access", "completion"] | None = None
    """Which timer fired. None unless `timed_out`."""

    scenario_name: str | None = None
    """Name of the scenario that fired."""

    trigger_reason: str | None = None
    """What matched the trigger (e.g. URL pattern, element appeared)."""

    completion_reason: str | None = None
    """What matched the completion. None if not blocked or if timed out."""

    duration: float = 0.0
    """Seconds spent waiting for the human (0 if not blocked)."""


@dataclass
class Handoff:
    """Reusable handoff orchestrator.

    Holds the transport config (server, notifiers, viewport) and is
    shared across many pages/runs. Two entry points:

      - `guard(page, scenarios=...)` — watch a page for triggers; on
        match, stream the page to a human and wait for completion.
        Use when the library should decide *when* a human is needed.
      - `pause(page, until=...)` — stream the page to a
        human now and pause until `until` matches. Use when the caller
        has already decided a human is needed (e.g. an agent tool).

    Pass scenarios per-call to `guard`. The `scenarios` constructor arg
    is deprecated.

    The streaming server is shared across all handoffs on this instance
    — it starts lazily on the first handoff and stops when the last one
    finishes. Concurrent handoffs run as distinct sessions on one port.

    Example:
        h = Handoff(notifiers=[DiscordNotifier(...)])

        result = await h.guard(
            page,
            scenarios=[
                Scenario(
                    name="login_required",
                    on=Detection.element(present=['input[type="email"]']),
                    until=Detection.url(path_contains=["/dashboard"]),
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

    # Shared streaming-server state, ref-counted via _acquire_server /
    # _release_server. _server_lock serializes start/stop so concurrent
    # handoffs neither double-bind the port nor overlap start with stop.
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

        Scenarios are assigned after construction to avoid the deprecated
        constructor-arg warning.
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

    async def guard(
        self,
        page: "Page",
        *,
        scenarios: list[Scenario] | None = None,
        trigger_timeout: float = 30.0,
        access_timeout: float | None = None,
        completion_timeout: float | None = None,
        stream_url: str | None = None,
    ) -> "HandoffResult":
        """Guard a page with scenarios; hand off on trigger match.

        Registers listeners on every scenario's trigger and waits for
        one to fire (within `trigger_timeout`) or for it to elapse.

        Args:
            page: Playwright page to monitor.
            scenarios: Trigger-completion pairs to watch. Falls back to
                the scenarios set on the instance. ValueError if neither.
            trigger_timeout: Max seconds to wait for any trigger. Does
                NOT bound the human-completion phase.
            access_timeout: Per-call override for the pre-connect bound.
                None inherits `ServerConfig.access_timeout`.
            completion_timeout: Per-call override for the post-connect
                work budget. None inherits `ServerConfig.completion_timeout`.
            stream_url: Optional substrate viewer URL. When set, the
                handoff runs in passthrough mode and `stream_url` is
                forwarded to `pause`.

        Returns:
            HandoffResult describing what happened. Never raises on
            handoff-phase timeout — check `result.timed_out` and
            `result.timeout_cause`.
        """
        scenarios = scenarios if scenarios is not None else self.scenarios
        if not scenarios:
            raise ValueError(
                "guard() requires at least one scenario: pass scenarios=[...] "
                "or set them on Handoff(...). To stream without a trigger, use "
                "pause()."
            )

        # LLMDetection in a trigger tree is misuse: no operator yet, no
        # reason for the prompt, and only DOM-noise signal — a
        # vision-call-per-mutation hot loop on real sites. Reject early
        # with the scenario name; walk to catch combinator nesting.
        for scenario in scenarios:
            if _detection_tree_has_llm(scenario.on):
                raise TypeError(
                    f"Scenario {scenario.name!r} uses LLMDetection in its "
                    "trigger (possibly nested inside a combinator). "
                    "LLMDetection is only valid as a completion check via "
                    "Handoff.pause (or as Scenario.complete). "
                    "Use URL/Element/Content detections for triggers."
                )

        trigger_event = asyncio.Event()
        matched_scenario: Scenario | None = None
        matched_result: DetectionResult | None = None
        detection_to_scenario: dict[int, Scenario] = {
            id(s.on): s for s in scenarios
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
            s.on.register_listeners(page, on_trigger) for s in scenarios
        ]

        try:
            # Initial check — page may already be in a triggered state when
            # called (e.g. caller awaited goto() then immediately ran us).
            for scenario in scenarios:
                r = await scenario.on.check(page)
                if r.matched:
                    matched_scenario = scenario
                    matched_result = r
                    trigger_event.set()
                    break

            if not trigger_event.is_set():
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        trigger_event.wait(), timeout=trigger_timeout
                    )

            if matched_scenario is None or matched_result is None:
                logger.info(
                    "handoff.guard: no trigger matched within %.1fs (page url=%s)",
                    trigger_timeout, page.url,
                )
                return HandoffResult(was_blocked=False)

            logger.info(
                "handoff.guard: trigger matched (scenario='%s'): %s",
                matched_scenario.name, matched_result.reason,
            )
            return await self.pause(
                page,
                matched_scenario.until,
                reason=matched_result.reason,
                name=matched_scenario.name,
                stream_url=stream_url,
                access_timeout=access_timeout,
                completion_timeout=completion_timeout,
            )
        finally:
            for cleanup in cleanups:
                with suppress(Exception):
                    cleanup()

    async def run(
        self,
        page: "Page",
        *,
        scenarios: list[Scenario] | None = None,
        trigger_timeout: float = 30.0,
        access_timeout: float | None = None,
        completion_timeout: float | None = None,
        stream_url: str | None = None,
        timeout: float | None = None,
    ) -> "HandoffResult":
        """Deprecated alias for :meth:`guard`.

        Also accepts the deprecated `timeout=` kwarg — v0.6 called this
        method with `timeout=`, so the shim forwards it to
        `trigger_timeout` with a separate warning.
        """
        warnings.warn(
            "Handoff.run() is deprecated; use `guard()`. Will be "
            "removed in a future major release.",
            DeprecationWarning,
            stacklevel=2,
        )
        if timeout is not None:
            warnings.warn(
                "Handoff.run(timeout=...) is deprecated; use "
                "`guard(trigger_timeout=...)`. Will be removed in a "
                "future major release.",
                DeprecationWarning,
                stacklevel=2,
            )
            trigger_timeout = timeout
        return await self.guard(
            page,
            scenarios=scenarios,
            trigger_timeout=trigger_timeout,
            access_timeout=access_timeout,
            completion_timeout=completion_timeout,
            stream_url=stream_url,
        )

    async def pause(
        self,
        page: "Page",
        until: BaseDetection,
        *,
        reason: str = "Human intervention required",
        name: str = "handoff",
        stream_url: str | None = None,
        access_timeout: float | None = None,
        completion_timeout: float | None = None,
    ) -> "HandoffResult":
        """Stream the page to a human *now* and pause until `until` matches.

        Skips trigger detection — use when the caller has already
        decided a human is needed. `guard()` funnels here on trigger match.

        Args:
            page: Playwright page to stream.
            until: Resume condition; the handoff returns the moment it
                matches (or on entry if the page already satisfies it).
            reason: Operator-facing explanation shown in the wrapper and
                notifications.
            name: Label recorded on `HandoffResult.scenario_name`.
            stream_url: Optional substrate viewer URL. When set, the
                wrapper iframes this URL; bh still owns detection,
                notification, and lifecycle.
            access_timeout: Per-call override of the pre-connect bound.
                None inherits `ServerConfig.access_timeout`.
            completion_timeout: Per-call override of the post-connect
                work budget. None inherits `ServerConfig.completion_timeout`.

        Returns:
            HandoffResult with `was_blocked=True`. Check `timed_out` /
            `timeout_cause` for which timer fired. Never raises on timeout.
        """
        context = page.context
        start_time = time.time()
        session_id = str(uuid.uuid4())[:8]
        listener_cleanups: list[Any] = []
        completion_event = asyncio.Event()
        completion_reason: str | None = None

        resolved_access = _resolve_timeout(access_timeout, self.server.access_timeout)
        resolved_completion = _resolve_timeout(
            completion_timeout, self.server.completion_timeout
        )

        # Closure cell — the gated callback is defined before the
        # session exists; we patch it in once register_session returns.
        session_ref: dict[str, Any] = {"session": None}

        async def on_completion_detected(detection: BaseDetection) -> None:
            nonlocal completion_reason
            if completion_event.is_set():
                return
            session = session_ref["session"]
            # Presence gate: a detection may fire on its own schedule
            # (LLMDetection's idle-settle + safety-net) after the
            # operator wandered off — skip the check until they're back.
            if session is not None and session.presence.state != "present":
                return
            # session.reason is the string the operator sees in the
            # wrapper header — the right framing for LLMDetection's
            # prompt. Other detections accept and ignore via **context.
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

            # Page-rect-on-display metrics for the passthrough template's
            # iframe crop. Only used in passthrough mode.
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
                access_timeout=resolved_access,
                completion_timeout=resolved_completion,
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

            # Already complete? (E.g. page raced past completion before
            # listeners are set up.) Skip the initial probe when `until`
            # is LLM-shaped — vision calls before the wrapper loads are
            # wasted; only cheap non-LLM checks run here.
            if not _detection_tree_has_llm(until):
                initial = await until.check(page, reason=reason)
                if initial.matched:
                    completion_reason = initial.reason
                    completion_event.set()

            # Lazy install gates listener registration on first connect
            # (substrate-URL leak defense). The three-way race below
            # starts immediately so access_timeout can fire before any
            # connect; a separate side task arms listeners on connect.
            timeout_cause: Literal["access", "completion"] | None = None

            async def install_listeners_after_connect() -> None:
                nonlocal completion_reason
                await session.presence.wait_until_connected()
                if completion_event.is_set():
                    return
                # Race defense: state may have been reached between the
                # initial probe (T0) and first-connect. Listeners only
                # fire on new events, so any transition that already
                # happened would be missed. Re-probe here — now with LLM
                # included, since the wrapper has loaded and vision
                # calls are no longer wasted.
                arrival = await until.check(page, reason=session.reason)
                if arrival.matched:
                    completion_reason = arrival.reason
                    completion_event.set()
                    return
                listener_cleanups.append(
                    until.register_listeners(page, on_completion_detected)
                )

            listener_install_task = asyncio.create_task(
                install_listeners_after_connect()
            )
            try:
                if not completion_event.is_set():
                    timeout_cause = await self._await_timeout_cause(
                        session, completion_event
                    )
            except asyncio.CancelledError:
                # Caller (per-step timeout, ctrl-c, explicit cancel)
                # gave up. Push a task_cancelled event so the wrapper
                # shows "the agent gave up" instead of "you ran out of
                # time" — without it, a passthrough iframe would also
                # stay interactive against a substrate bh no longer
                # owns. Re-raise to preserve cancellation semantics.
                with suppress(Exception):
                    await server.notify_task_cancelled(session_id)
                raise
            finally:
                listener_install_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await listener_install_task

            timed_out = timeout_cause is not None
            if timed_out:
                logger.warning(
                    "Handoff %s_timeout fired", timeout_cause,
                )

            await server.stop_screencast(session_id)

            if timed_out:
                await server.notify_task_expired(session_id)
            elif completion_reason:
                await server.notify_task_completed(session_id, completion_reason)

            return HandoffResult(
                was_blocked=True,
                timed_out=timed_out,
                timeout_cause=timeout_cause,
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

    async def wait_for_completion(
        self,
        page: "Page",
        on: BaseDetection,
        *,
        reason: str = "Human intervention required",
        name: str = "handoff",
        stream_url: str | None = None,
        access_timeout: float | None = None,
        completion_timeout: float | None = None,
    ) -> "HandoffResult":
        """Deprecated alias for :meth:`pause`."""
        warnings.warn(
            "Handoff.wait_for_completion() is deprecated; use "
            "`pause()`. Will be removed in a future major release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.pause(
            page,
            on,
            reason=reason,
            name=name,
            stream_url=stream_url,
            access_timeout=access_timeout,
            completion_timeout=completion_timeout,
        )

    @staticmethod
    async def _await_timeout_cause(
        session: "Any", completion_event: asyncio.Event
    ) -> Literal["access", "completion"] | None:
        """Return the timer that fired, or None if detection matched first.

        - "access": pre-first-connect window expired.
        - "completion": post-first-connect work budget expired.
        - None: detection matched (completion_event set).

        Sets `session.access_timer_fired` right before returning "access"
        so the WS guard can reject late operator clicks.
        """
        async def access_timeout_branch() -> Literal["access"]:
            if session.access_timeout is None:
                await asyncio.Event().wait()  # never fires
            try:
                await asyncio.wait_for(
                    session.presence.wait_until_connected(),
                    timeout=session.access_timeout,
                )
                # Connect won; retire. Block until outer cancels us.
                await asyncio.Event().wait()
            except asyncio.TimeoutError:
                session.access_timer_fired = True
                return "access"
            # unreachable
            return "access"

        async def completion_timeout_branch() -> Literal["completion"]:
            await session.presence.wait_until_connected()
            if session.completion_timeout is None:
                await asyncio.Event().wait()  # never fires
            await asyncio.sleep(session.completion_timeout)
            return "completion"

        async def match_branch() -> None:
            await completion_event.wait()
            return None

        tasks = [
            asyncio.create_task(access_timeout_branch()),
            asyncio.create_task(completion_timeout_branch()),
            asyncio.create_task(match_branch()),
        ]
        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            return done.pop().result()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await t

    async def _acquire_server(self) -> StreamingServer:
        """Return the shared streaming server, starting it on first use.

        Reference-counted: every acquire must pair with a `_release_server`
        in `finally`. Start happens under the lock so concurrent first
        handoffs don't double-bind the port.
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

        On the last release the server stops under the lock, so a
        concurrent acquire waits for the port to release before
        re-binding. Start and stop never overlap.
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
                # server.stop() already closed connections and signaled
                # should_exit — let uvicorn unwind on its own, cancel
                # only as a last resort if it hangs.
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

        Each attempt has its own short cap — without it, a single
        connect stalled mid-handshake (uvicorn between bind and accept,
        WSL2 loopback quirks, firewall holding the SYN) would hang past
        the outer deadline.
        """
        # 0.0.0.0 / :: are bind addresses, not connect addresses.
        connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else (
            "::1" if host == "::" else host
        )
        # `interval` is the retry pause after a failure; `per_attempt`
        # caps a single try so a stuck connect can't starve the loop.
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
        # Fall back to a console panel when no notifiers are configured
        # — the operator still needs the URL somewhere. Caller-supplied
        # notifiers replace it; we don't double-push to stdout.
        notifiers = self.notifiers or [ConsoleNotifier()]

        title = "Human Intervention Required"
        # Structured items let each notifier render natively (Rich
        # markup, Discord embed, Slack mrkdwn, HTML <a>) instead of
        # parsing a flat string.
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
