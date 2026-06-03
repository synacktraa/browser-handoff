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

        async def on_completion_detected(detection: BaseDetection) -> None:
            nonlocal completion_reason
            if completion_event.is_set():
                return
            result = await detection.check(page)
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

            await server.register_session(
                session_id=session_id,
                page=page,
                context=context,
                reason=reason,
                scenario_name=name,
                viewport_size=viewport_size,
            )

            listener_cleanups.append(
                on.register_listeners(page, on_completion_detected)
            )

            stream_url = server.get_stream_url(session_id)

            logger.info("=" * 70)
            logger.info("HANDOFF: Human intervention required")
            logger.info("Reason: %s", reason)
            logger.info("Scenario: %s", name)
            logger.info("Stream URL: %s", stream_url)
            logger.info("=" * 70)

            await self._send_notifications(reason, stream_url)

            # Already complete? (e.g. page raced past completion before we
            # finished setting up listeners.)
            initial = await on.check(page)
            if initial.matched:
                completion_reason = initial.reason
                completion_event.set()

            timed_out = False
            try:
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

            await server.stop_screencast(session_id)

            if not timed_out and completion_reason:
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
        """
        # 0.0.0.0 / :: are bind addresses, not connect addresses.
        connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else (
            "::1" if host == "::" else host
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                _, writer = await asyncio.open_connection(connect_host, port)
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(interval)
        logger.warning(
            "Streaming server did not accept connections on %s:%d within %.1fs",
            connect_host, port, timeout,
        )

    async def _send_notifications(self, reason: str, stream_url: str) -> None:
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
            LinkItem(prefix="Stream URL: ", url=stream_url),
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
