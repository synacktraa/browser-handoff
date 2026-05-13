"""Main Handoff class and guard context manager."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator

from jinja2 import Environment, FileSystemLoader

from .config import load_file
from .detection import BaseDetection, Detection, DetectionResult
from .notifiers import Notifier, notifier_from_dict
from .scenario import Scenario
from .server import ServerConfig, StreamingServer

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

# Template directory for notifications
TEMPLATE_DIR = Path(__file__).parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)

# Default viewport size for the browser
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class CompletionResult:
    """Result of a handoff session completion."""

    success: bool
    reason: str
    detection_type: str
    matched_detection: BaseDetection | None = None
    duration: float = 0.0


class HandoffError(Exception):
    """Error during handoff process."""

    pass


@dataclass
class Handoff:
    """Main handoff orchestrator.

    Monitors a Playwright page for trigger conditions and hands off to humans
    when automation gets blocked. Each scenario defines a trigger-completion
    pair - when a trigger is detected, its corresponding completion condition
    is used.

    At least one scenario must be provided.

    Example:
        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="cloudflare_challenge",
                    trigger=Detection.content(title_contains=["Just a moment"]),
                    complete=Detection.element(missing=[".cf-turnstile"]),
                ),
                Scenario(
                    name="login_required",
                    trigger=Detection.url(path_contains=["/login"]),
                    complete=Detection.url(path_contains=["/dashboard"]),
                ),
            ],
            server=ServerConfig(port=8080),
        )

        async with handoff.guard(page) as session:
            await page.click("#login")
            # Auto-detects blockers and streams if needed
    """

    scenarios: list[Scenario] = field(default_factory=list)
    server: ServerConfig = field(default_factory=ServerConfig)
    notifiers: list[Notifier] = field(default_factory=list)
    viewport_size: dict[str, int] = field(default_factory=lambda: DEFAULT_VIEWPORT.copy())

    def __post_init__(self):
        """Validate that at least one scenario is provided."""
        if not self.scenarios:
            raise ValueError("At least one scenario must be provided")

    @classmethod
    def from_file(cls, path: str | Path) -> "Handoff":
        """Create Handoff from a JSON or YAML config file.

        Args:
            path: Path to configuration file.

        Returns:
            Configured Handoff instance.
        """
        config = load_file(path)
        return cls.from_dict(config)

    @classmethod
    def from_json(cls, json_string: str) -> "Handoff":
        """Create Handoff from a JSON string.

        Args:
            json_string: JSON configuration string.

        Returns:
            Configured Handoff instance.
        """
        from .config import load_json

        config = load_json(json_string)
        return cls.from_dict(config)

    @classmethod
    def from_yaml(cls, yaml_string: str) -> "Handoff":
        """Create Handoff from a YAML string.

        Args:
            yaml_string: YAML configuration string.

        Returns:
            Configured Handoff instance.
        """
        from .config import load_yaml

        config = load_yaml(yaml_string)
        return cls.from_dict(config)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Handoff":
        """Create Handoff from dictionary configuration.

        Args:
            data: Configuration dictionary.

        Returns:
            Configured Handoff instance.

        Raises:
            ValueError: If no scenarios are provided.
        """
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

    async def is_blocked(
        self, page: "Page"
    ) -> tuple[bool, DetectionResult | None, Scenario | None]:
        """Check if the page is currently blocked by any scenario trigger.

        Args:
            page: Playwright page to check.

        Returns:
            Tuple of (is_blocked, detection_result, matched_scenario).
        """
        for scenario in self.scenarios:
            result = await scenario.trigger.check(page)
            if result.matched:
                return True, result, scenario

        return False, None, None

    async def is_complete(
        self, page: "Page", scenario: Scenario
    ) -> tuple[bool, DetectionResult | None]:
        """Check if the scenario's completion condition is met.

        Args:
            page: Playwright page to check.
            scenario: The scenario whose completion condition to check.

        Returns:
            Tuple of (is_complete, detection_result).
        """
        result = await scenario.complete.check(page)
        if result.matched:
            return True, result
        return False, None

    async def wait_for_human(
        self,
        page: "Page",
        context: "BrowserContext",
        scenario: Scenario,
        reason: str = "Human intervention required",
    ) -> CompletionResult:
        """Start handoff session and wait for human completion.

        Args:
            page: Playwright page to stream.
            context: Browser context for CDP session.
            scenario: The scenario that triggered the handoff.
            reason: Reason shown to human.

        Returns:
            CompletionResult with details of completion.

        Raises:
            HandoffError: If handoff times out or fails.
        """
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
            # Start the streaming server
            server = StreamingServer(config=self.server)
            server_task = asyncio.create_task(server.start())

            # Wait for server to start
            await asyncio.sleep(0.5)

            # Get actual viewport size from page, fallback to configured size
            viewport_size = self.viewport_size
            try:
                actual_viewport = page.viewport_size
                if actual_viewport:
                    viewport_size = actual_viewport
                    logger.info(f"Using page viewport size: {viewport_size}")
                else:
                    # No viewport set, get actual window dimensions via JS
                    dimensions = await page.evaluate(
                        "() => ({ width: window.innerWidth, height: window.innerHeight })"
                    )
                    if dimensions and dimensions.get("width") and dimensions.get("height"):
                        viewport_size = dimensions
                        logger.info(f"Using window dimensions: {viewport_size}")
                    else:
                        logger.info(f"Could not get window size, using default: {viewport_size}")
            except Exception as e:
                logger.info(f"Could not get viewport: {e}, using default: {viewport_size}")

            # Register session
            await server.register_session(
                session_id=session_id,
                page=page,
                context=context,
                reason=reason,
                viewport_size=viewport_size,
            )

            # Register completion listener for the scenario's completion condition
            logger.info(f"Using scenario '{scenario.name}' completion condition")
            cleanup = scenario.complete.register_listeners(page, on_completion_detected)
            listener_cleanups.append(cleanup)

            # Generate stream URL
            stream_url = server.get_stream_url(session_id)

            # Log handoff info
            logger.info("=" * 70)
            logger.info("HANDOFF: Human intervention required")
            logger.info("=" * 70)
            logger.info("Reason: %s", reason)
            logger.info("Scenario: %s", scenario.name)
            logger.info("Stream URL: %s", stream_url)
            logger.info("=" * 70)

            # Send notifications
            await self._send_notifications(reason, stream_url)

            # Check completion immediately in case already complete
            is_complete, result = await self.is_complete(page, scenario)
            logger.info(f"Initial completion check: is_complete={is_complete}, result={result}")
            if is_complete and result:
                logger.info(f"Completion detected immediately: {result.reason}")
                completion_result = CompletionResult(
                    success=True,
                    reason=result.reason,
                    detection_type=result.detection_type,
                    duration=time.time() - start_time,
                )
                completion_event.set()

            # Wait for completion or timeout
            logger.info(f"Waiting for completion event (timeout={self.server.timeout}s)...")
            try:
                await asyncio.wait_for(
                    completion_event.wait(),
                    timeout=self.server.timeout,
                )
                logger.info("Completion event received!")
            except asyncio.TimeoutError:
                raise HandoffError(
                    f"Handoff timeout: user did not complete task within {self.server.timeout}s"
                )

            # Stop screencast before sensitive data appears
            logger.info("Stopping screencast...")
            await server.stop_screencast(session_id)

            # Notify completion
            if completion_result:
                logger.info(f"Notifying task completed: {completion_result.reason}")
                await server.notify_task_completed(session_id, completion_result.reason)
                await asyncio.sleep(0.5)  # Give time for message to be sent

            logger.info(f"Returning completion result: {completion_result}")
            return completion_result or CompletionResult(
                success=False,
                reason="Unknown completion",
                detection_type="unknown",
                duration=time.time() - start_time,
            )

        finally:
            # Cleanup listeners
            for cleanup in listener_cleanups:
                with suppress(Exception):
                    cleanup()

            # Cleanup server
            if server:
                await server.unregister_session(session_id)
                await server.stop()

            if server_task and not server_task.done():
                server_task.cancel()
                with suppress(asyncio.CancelledError):
                    await server_task

    async def _send_notifications(self, reason: str, stream_url: str) -> None:
        """Send notifications to all configured notifiers."""
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

    @asynccontextmanager
    async def guard(
        self,
        page: "Page",
        context: "BrowserContext | None" = None,
    ) -> AsyncGenerator["GuardedSession", None]:
        """Context manager that monitors page and triggers handoff when needed.

        Args:
            page: Playwright page to monitor.
            context: Optional browser context (auto-detected if not provided).

        Yields:
            GuardedSession for tracking state.

        Example:
            async with handoff.guard(page) as session:
                await page.click("#login")
                # Automatically handles blockers
        """
        # Get context from page if not provided
        if context is None:
            context = page.context

        session = GuardedSession(handoff=self, page=page, context=context)
        listener_cleanups: list[Any] = []

        # Map detections to their scenarios for event-based triggering
        detection_to_scenario: dict[int, Scenario] = {}
        for scenario in self.scenarios:
            detection_to_scenario[id(scenario.trigger)] = scenario

        async def on_trigger_detected(detection: BaseDetection) -> None:
            if session.in_handoff:
                return

            result = await detection.check(page)
            if result.matched:
                session.in_handoff = True
                session.trigger_reason = result.reason

                # Find the matched scenario
                matched_scenario = detection_to_scenario[id(detection)]
                session.matched_scenario = matched_scenario

                logger.info(
                    "Trigger detected (scenario '%s'): %s",
                    matched_scenario.name,
                    result.reason,
                )

                # Perform handoff with matched scenario
                try:
                    completion = await self.wait_for_human(
                        page=page,
                        context=context,
                        scenario=matched_scenario,
                        reason=result.reason,
                    )
                    session.completion_result = completion
                finally:
                    session.in_handoff = False

        try:
            # Register trigger listeners for all scenarios
            for scenario in self.scenarios:
                cleanup = scenario.trigger.register_listeners(page, on_trigger_detected)
                listener_cleanups.append(cleanup)

            # Check for triggers immediately
            is_blocked, result, matched_scenario = await self.is_blocked(page)
            if is_blocked and result and matched_scenario:
                session.in_handoff = True
                session.trigger_reason = result.reason
                session.matched_scenario = matched_scenario

                logger.info(
                    "Initial trigger detected (scenario '%s'): %s",
                    matched_scenario.name,
                    result.reason,
                )

                completion = await self.wait_for_human(
                    page=page,
                    context=context,
                    scenario=matched_scenario,
                    reason=result.reason,
                )
                session.completion_result = completion
                session.in_handoff = False

            yield session

        finally:
            # Cleanup listeners
            for cleanup in listener_cleanups:
                with suppress(Exception):
                    cleanup()


@dataclass
class GuardedSession:
    """State tracker for a guarded page session."""

    handoff: Handoff
    page: "Page"
    context: "BrowserContext"
    in_handoff: bool = False
    trigger_reason: str | None = None
    completion_result: CompletionResult | None = None
    matched_scenario: Scenario | None = None

    @property
    def was_blocked(self) -> bool:
        """Check if the session was blocked at any point."""
        return self.completion_result is not None

    @property
    def completed_successfully(self) -> bool:
        """Check if handoff completed successfully."""
        return self.completion_result is not None and self.completion_result.success

    @property
    def scenario_name(self) -> str | None:
        """Get the name of the matched scenario, if any."""
        return self.matched_scenario.name if self.matched_scenario else None
