"""Main Handoff class — single-method API for human-in-the-loop fallback."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader

from .config import load_file
from .detection import BaseDetection, Detection, DetectionResult
from .notifiers import Notifier, notifier_from_dict
from .scenario import Scenario
from .server import ServerConfig, StreamingServer

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class CompletionResult:
    """Result of a handoff session completion."""

    success: bool
    reason: str
    detection_type: str
    matched_detection: BaseDetection | None = None
    duration: float = 0.0


@dataclass
class HandoffResult:
    """Result of Handoff.run() call."""

    was_blocked: bool
    """Whether human intervention was required."""

    scenario_name: str | None
    """Name of the scenario that triggered, if any."""

    trigger_reason: str | None
    """Reason the trigger matched, if any."""

    completion_result: CompletionResult | None
    """Details of completion, if blocked."""


class HandoffError(Exception):
    """Error during handoff process."""

    pass


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
                    trigger=Detection.element(selector='input[type="email"]'),
                    complete=Detection.url(path_contains=["/dashboard"]),
                ),
            ],
        )

        result = await handoff.run(page)
        if result.was_blocked:
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
        config = load_file(path)
        return cls.from_dict(config)

    @classmethod
    def from_json(cls, json_string: str) -> "Handoff":
        from .config import load_json

        config = load_json(json_string)
        return cls.from_dict(config)

    @classmethod
    def from_yaml(cls, yaml_string: str) -> "Handoff":
        from .config import load_yaml

        config = load_yaml(yaml_string)
        return cls.from_dict(config)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Handoff":
        scenarios = [Scenario.from_dict(s) for s in data.get("scenarios", [])]

        server_data = data.get("server", {})
        server = ServerConfig.from_dict(server_data) if server_data else ServerConfig()

        notifiers_data = data.get("notifiers", [])
        notifiers = [notifier_from_dict(n) for n in notifiers_data]

        return cls(
            scenarios=scenarios,
            server=server,
            notifiers=notifiers,
        )

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
            scenario's completion condition (bounded by self.server.timeout).
          - `timeout` seconds to elapse with no trigger → return was_blocked=False.

        The browser context is auto-detected from page.context — there's no
        case where you'd want a different one for the same page.

        Args:
            page: Playwright page to monitor.
            timeout: Max seconds to wait for any trigger to fire before
                concluding no handoff is needed. Default: 30.0.
                Does NOT bound the human-completion phase — that uses
                self.server.timeout (default 600s, set on ServerConfig).

        Returns:
            HandoffResult describing what happened.
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
                try:
                    await asyncio.wait_for(trigger_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass

            if matched_scenario is None or matched_result is None:
                logger.info(
                    "handoff.run: no trigger matched within %.1fs (page url=%s)",
                    timeout, page.url,
                )
                return HandoffResult(
                    was_blocked=False,
                    scenario_name=None,
                    trigger_reason=None,
                    completion_result=None,
                )

            logger.info(
                "handoff.run: trigger matched (scenario='%s'): %s",
                matched_scenario.name, matched_result.reason,
            )
            completion = await self._run_handoff(
                page=page,
                context=context,
                scenario=matched_scenario,
                reason=matched_result.reason,
            )
            return HandoffResult(
                was_blocked=True,
                scenario_name=matched_scenario.name,
                trigger_reason=matched_result.reason,
                completion_result=completion,
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
        reason: str,
    ) -> CompletionResult:
        """Stream the page to a human, wait for the scenario's completion."""
        import time

        start_time = time.time()
        session_id = str(uuid.uuid4())[:8]
        server: StreamingServer | None = None
        server_task: asyncio.Task[None] | None = None
        listener_cleanups: list[Any] = []
        completion_event = asyncio.Event()
        completion_result: CompletionResult | None = None

        async def on_completion_detected(detection: BaseDetection) -> None:
            nonlocal completion_result
            if completion_event.is_set():
                return

            result = await detection.check(page)
            if result.matched:
                completion_result = CompletionResult(
                    success=True,
                    reason=result.reason,
                    detection_type=result.detection_type,
                    matched_detection=detection,
                    duration=time.time() - start_time,
                )
                completion_event.set()

        try:
            server = StreamingServer(config=self.server)
            server_task = asyncio.create_task(server.start())
            await asyncio.sleep(0.5)  # let the server bind

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
                viewport_size=viewport_size,
            )

            cleanup = scenario.complete.register_listeners(page, on_completion_detected)
            listener_cleanups.append(cleanup)

            stream_url = server.get_stream_url(session_id)

            logger.info("=" * 70)
            logger.info("HANDOFF: Human intervention required")
            logger.info("Reason: %s", reason)
            logger.info("Scenario: %s", scenario.name)
            logger.info("Stream URL: %s", stream_url)
            logger.info("=" * 70)

            await self._send_notifications(reason, stream_url)

            # Already complete? (e.g. page raced past completion before we
            # finished setting up listeners.)
            initial = await scenario.complete.check(page)
            if initial.matched:
                completion_result = CompletionResult(
                    success=True,
                    reason=initial.reason,
                    detection_type=initial.detection_type,
                    duration=time.time() - start_time,
                )
                completion_event.set()

            try:
                await asyncio.wait_for(
                    completion_event.wait(),
                    timeout=self.server.timeout,
                )
            except asyncio.TimeoutError:
                raise HandoffError(
                    f"Handoff timeout: user did not complete task within {self.server.timeout}s"
                )

            await server.stop_screencast(session_id)

            if completion_result:
                await server.notify_task_completed(session_id, completion_result.reason)
                await asyncio.sleep(0.5)  # give the message time to send

            return completion_result or CompletionResult(
                success=False,
                reason="Unknown completion",
                detection_type="unknown",
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
                # cancel as a last resort if it hangs (shouldn't happen).
                try:
                    await asyncio.wait_for(server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    server_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await server_task
                except asyncio.CancelledError:
                    pass

    async def _send_notifications(self, reason: str, stream_url: str) -> None:
        if not self.notifiers:
            return

        notification_template = jinja_env.get_template("notification.jinja")
        message = notification_template.render(reason=reason, stream_url=stream_url)
        title = "Human Intervention Required"

        async def send_notification(notifier: Notifier) -> None:
            try:
                await notifier.send(
                    title=title,
                    message=message,
                    urgency="critical",
                )
            except Exception as e:
                logger.error(
                    "Failed to send notification via %s: %s",
                    type(notifier).__name__,
                    e,
                )

        await asyncio.gather(
            *[send_notification(n) for n in self.notifiers],
            return_exceptions=True,
        )
