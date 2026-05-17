"""Main Handoff class — single-method API for human-in-the-loop fallback."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader

from .config import load_file, load_json, load_yaml
from .detection.base import BaseDetection, DetectionResult
from .notifiers import ConsoleNotifier, Notifier, notifier_from_dict
from .scenario import Scenario
from .server import ServerConfig, StreamingServer

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
# autoescape=False: the only template loaded here is notification.jinja,
# which renders to plain text for Discord/Slack/email/rich-console.
# HTML-escaping a single quote into `&#39;` mangles the URL the operator
# is supposed to click. The HTML client template uses its own jinja env
# (in server/streaming.py) which keeps autoescape on, as it should.
jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=False)

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class HandoffResult:
    """Outcome of a Handoff.run() call.

    Three terminal states:
      - was_blocked=False                  → no trigger fired within timeout
      - was_blocked=True, timed_out=False  → human completed the task
      - was_blocked=True, timed_out=True   → human exceeded completion_timeout
    """

    was_blocked: bool
    """Whether a trigger fired and a human handoff was performed."""

    timed_out: bool = False
    """Only meaningful if was_blocked: human exceeded completion_timeout."""

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

    Watch a Playwright page for trigger conditions, and when one fires, stream
    the page to a human and wait for them to complete a corresponding action.

    Example:
        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="login_required",
                    trigger=Detection.element(present=['input[type="email"]']),
                    complete=Detection.url(path_contains=["/dashboard"]),
                ),
            ],
        )

        result = await handoff.run(page)
        if result.was_blocked and not result.timed_out:
            print(f"Human completed: {result.scenario_name}")
        await bot_logic(page)
    """

    scenarios: list[Scenario] = field(default_factory=list)
    server: ServerConfig = field(default_factory=ServerConfig)
    notifiers: list[Notifier] = field(default_factory=list)
    viewport_size: dict[str, int] = field(default_factory=lambda: DEFAULT_VIEWPORT.copy())

    def __post_init__(self):
        if not self.scenarios:
            raise ValueError("At least one scenario must be provided")

    @classmethod
    def from_file(cls, path: str | Path) -> "Handoff":
        return cls.from_dict(load_file(path))

    @classmethod
    def from_json(cls, json_string: str) -> "Handoff":
        return cls.from_dict(load_json(json_string))

    @classmethod
    def from_yaml(cls, yaml_string: str) -> "Handoff":
        return cls.from_dict(load_yaml(yaml_string))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Handoff":
        scenarios = [Scenario.from_dict(s) for s in data.get("scenarios", [])]
        server_data = data.get("server", {})
        server = ServerConfig.from_dict(server_data) if server_data else ServerConfig()
        notifiers = [notifier_from_dict(n) for n in data.get("notifiers", [])]
        return cls(scenarios=scenarios, server=server, notifiers=notifiers)

    async def run(
        self,
        page: "Page",
        *,
        timeout: float = 30.0,
    ) -> "HandoffResult":
        """Wait until handoff completes, or no handoff is needed.

        Registers framenavigated/element-mutation listeners on every scenario's
        trigger, then waits for either:
          - A trigger to fire → run the human handoff and wait for the
            scenario's completion condition (bounded by
            self.server.completion_timeout).
          - `timeout` seconds to elapse with no trigger → return was_blocked=False.

        The browser context is auto-detected from page.context — there's no
        case where you'd want a different one for the same page.

        Args:
            page: Playwright page to monitor.
            timeout: Max seconds to wait for any trigger to fire before
                concluding no handoff is needed. Default: 30.0.
                Does NOT bound the human-completion phase — that uses
                self.server.completion_timeout (default 600s, set on
                ServerConfig).

        Returns:
            HandoffResult describing what happened. Never raises on
            human-completion timeout — check result.timed_out instead.
        """
        context = page.context

        trigger_event = asyncio.Event()
        matched_scenario: Scenario | None = None
        matched_result: DetectionResult | None = None
        detection_to_scenario: dict[int, Scenario] = {
            id(s.trigger): s for s in self.scenarios
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
            s.trigger.register_listeners(page, on_trigger) for s in self.scenarios
        ]

        try:
            # Initial check — page may already be in a triggered state when
            # called (e.g. caller awaited goto() then immediately ran us).
            for scenario in self.scenarios:
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
            return await self._run_handoff(
                page=page,
                context=context,
                scenario=matched_scenario,
                trigger_reason=matched_result.reason,
            )
        finally:
            for cleanup in cleanups:
                with suppress(Exception):
                    cleanup()

    async def _run_handoff(
        self,
        page: "Page",
        context: Any,
        scenario: Scenario,
        trigger_reason: str,
    ) -> HandoffResult:
        """Stream the page to a human, wait for the scenario's completion."""
        start_time = time.time()
        session_id = str(uuid.uuid4())[:8]
        server: StreamingServer | None = None
        server_task: asyncio.Task[None] | None = None
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

        try:
            server = StreamingServer(config=self.server)
            server_task = asyncio.create_task(server.start())
            await self._wait_for_port(self.server.host, self.server.port)

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
                reason=trigger_reason,
                viewport_size=viewport_size,
            )

            listener_cleanups.append(
                scenario.complete.register_listeners(page, on_completion_detected)
            )

            stream_url = server.get_stream_url(session_id)

            logger.info("=" * 70)
            logger.info("HANDOFF: Human intervention required")
            logger.info("Reason: %s", trigger_reason)
            logger.info("Scenario: %s", scenario.name)
            logger.info("Stream URL: %s", stream_url)
            logger.info("=" * 70)

            await self._send_notifications(trigger_reason, stream_url)

            # Already complete? (e.g. page raced past completion before we
            # finished setting up listeners.)
            initial = await scenario.complete.check(page)
            if initial.matched:
                completion_reason = initial.reason
                completion_event.set()

            timed_out = False
            try:
                await asyncio.wait_for(
                    completion_event.wait(),
                    timeout=self.server.completion_timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning(
                    "Handoff completion_timeout: human did not finish "
                    "within %.0fs", self.server.completion_timeout,
                )

            await server.stop_screencast(session_id)

            if not timed_out and completion_reason:
                await server.notify_task_completed(session_id, completion_reason)

            return HandoffResult(
                was_blocked=True,
                timed_out=timed_out,
                scenario_name=scenario.name,
                trigger_reason=trigger_reason,
                completion_reason=None if timed_out else completion_reason,
                duration=time.time() - start_time,
            )

        finally:
            for cleanup in listener_cleanups:
                with suppress(Exception):
                    cleanup()

            if server:
                await server.unregister_session(session_id)
                await server.stop()

            if server_task and not server_task.done():
                # server.stop() already signaled should_exit and closed the
                # client connections. Let uvicorn unwind on its own; only
                # cancel as a last resort if it hangs.
                try:
                    await asyncio.wait_for(server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    server_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await server_task
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

        notification_template = jinja_env.get_template("notification.jinja")
        message = notification_template.render(reason=reason, stream_url=stream_url)
        title = "Human Intervention Required"

        async def send_notification(notifier: Notifier) -> None:
            try:
                await notifier.send(title=title, message=message, urgency="critical")
            except Exception as e:
                logger.error(
                    "Failed to send notification via %s: %s",
                    type(notifier).__name__, e,
                )

        await asyncio.gather(
            *[send_notification(n) for n in notifiers],
            return_exceptions=True,
        )
