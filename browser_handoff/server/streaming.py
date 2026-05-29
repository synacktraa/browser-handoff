"""Streaming server for human intervention via CDP screencast."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from .config import ServerConfig
from .session import DEFAULT_VIEWPORT, HandoffSession

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, CDPSession, Page

# Setup Jinja2 templates
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)

# Injected into a streamed page to suppress the native right-click context
# menu. That menu is an OS-level overlay the screencast can't capture, so a
# right-click would trap the operator in front of an invisible menu. Capture
# at the capture phase so it fires before page handlers. The flag is
# non-enumerable so it doesn't show up if the page enumerates window.
_CONTEXT_MENU_GUARD_JS = """
(() => {
  if (window.__bhContextGuard) return;
  Object.defineProperty(window, '__bhContextGuard',
    {value: true, enumerable: false, configurable: true});
  window.addEventListener('contextmenu', (e) => e.preventDefault(), true);
})();
"""


class StreamingServer:
    """Server that manages streaming sessions for human intervention.

    Example:
        server = StreamingServer(config=ServerConfig(port=8080))
        await server.start()

        # Register a session
        await server.register_session(
            session_id="abc123",
            page=page,
            context=context,
            reason="Login required",
        )

        # Wait for user to complete task
        # ...

        await server.stop()
    """

    def __init__(self, config: ServerConfig | None = None):
        """Initialize the streaming server.

        Args:
            config: Server configuration. If not provided, uses defaults.
        """
        self.config = config or ServerConfig()
        self.sessions: dict[str, HandoffSession] = {}
        self.app = self._create_app()
        self._server: uvicorn.Server | None = None

    def _create_app(self) -> FastAPI:
        """Create the FastAPI application."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            yield
            # Cleanup on shutdown
            for session in self.sessions.values():
                if session.capture_task and not session.capture_task.done():
                    session.capture_task.cancel()
                for task in list(session.background_tasks):
                    task.cancel()
            self.sessions.clear()

        app = FastAPI(title="Browser Handoff Stream", lifespan=lifespan)

        # No credentials are used (the stream is gated by the session id in the
        # URL, not cookies/auth headers), so wildcard origins are valid here.
        # allow_credentials must stay False: browsers reject "*" together with
        # credentials, and Starlette disallows that combination.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/", response_class=HTMLResponse, response_model=None)
        async def index(session: str = "default"):
            """Serve the HTML client."""
            session_state = self.sessions.get(session)
            if not session_state:
                return HTMLResponse("<h1>Session not found</h1>", status_code=404)

            # Mark session as accessed
            session_state.mark_accessed()

            return self._get_html_client(session, session_state.reason)

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket, session: str = "default"):
            """WebSocket endpoint: binary frames out, JSON control in."""
            await websocket.accept()

            session_state = self.sessions.get(session)
            if not session_state:
                await websocket.close()
                return

            session_state.websockets.append(websocket)
            cdp = session_state.cdp
            page = session_state.page

            # Push the most recent frame immediately so the client paints
            # something before the next screencast tick arrives.
            if session_state.latest_frame is not None:
                with suppress(Exception):
                    await websocket.send_bytes(session_state.latest_frame)

            sender_task = asyncio.create_task(
                self._stream_frames_to_ws(websocket, session_state)
            )

            try:
                with suppress(WebSocketDisconnect):
                    while True:
                        data = await websocket.receive_text()
                        message = json.loads(data)
                        msg_type = message.get("type")

                        try:
                            if msg_type == "mouse":
                                await self._handle_mouse(cdp, message)
                            elif msg_type == "keyboard":
                                await self._handle_keyboard(cdp, message)
                            elif msg_type == "navigate":
                                await self._handle_navigate(page, message)
                        except Exception as e:
                            logger.error(f"Error handling {msg_type} event: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            finally:
                sender_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await sender_task
                if websocket in session_state.websockets:
                    session_state.websockets.remove(websocket)

        return app

    async def _stream_frames_to_ws(
        self, websocket: WebSocket, session: HandoffSession
    ) -> None:
        """Push the latest frame to a single WS, dropping intermediate frames.

        Latest-frame-wins: if capture produces N new frames while we're
        awaiting send_bytes (slow client / tunnel / WAN), the next loop
        iteration sees only the newest one and skips the rest. That gives
        smooth playback under load instead of building a backlog.
        """
        last_seq = -1
        while True:
            async with session.frame_condition:
                await session.frame_condition.wait_for(
                    lambda: session.closed or session.frame_seq != last_seq
                )
                if session.closed:
                    return
                frame = session.latest_frame
                last_seq = session.frame_seq

            if frame is None:
                continue
            try:
                await websocket.send_bytes(frame)
            except Exception:
                return

    async def register_session(
        self,
        session_id: str,
        page: "Page",
        context: "BrowserContext",
        reason: str,
        viewport_size: dict[str, int] | None = None,
    ) -> HandoffSession:
        """Register a new Page for streaming.

        Args:
            session_id: Unique identifier for the session.
            page: Playwright page to stream.
            context: Browser context the page belongs to.
            reason: Reason for handoff (shown to user).
            viewport_size: Optional viewport dimensions.

        Returns:
            The created HandoffSession.
        """
        cdp = await context.new_cdp_session(page)
        await cdp.send("Page.enable")

        session = HandoffSession(
            session_id=session_id,
            page=page,
            context=context,
            cdp=cdp,
            reason=reason,
            viewport_size=viewport_size or DEFAULT_VIEWPORT.copy(),
        )
        self.sessions[session_id] = session

        await self._suppress_context_menu(page)

        # Take initial screenshot as first frame so the page paints
        # immediately when a client connects, before screencast warms up.
        with suppress(Exception):
            screenshot_bytes = await page.screenshot(
                type="jpeg", quality=self.config.jpeg_quality
            )
            await self._publish_frame(session, screenshot_bytes)

        # Start capture immediately
        session.capture_task = asyncio.create_task(self._capture_frames(session))

        return session

    @staticmethod
    async def _suppress_context_menu(page: "Page") -> None:
        """Block the native right-click menu on a streamed page.

        add_init_script covers documents loaded later (navigations during the
        handoff); evaluate covers the one already loaded. Both are best-effort
        — a transient failure must not abort the handoff.
        """
        with suppress(Exception):
            await page.add_init_script(_CONTEXT_MENU_GUARD_JS)
        with suppress(Exception):
            await page.evaluate(_CONTEXT_MENU_GUARD_JS)

    @staticmethod
    async def _publish_frame(session: HandoffSession, frame_bytes: bytes) -> None:
        """Publish a new frame and wake all per-WS senders."""
        async with session.frame_condition:
            session.latest_frame = frame_bytes
            session.frame_seq += 1
            session.frame_condition.notify_all()

    async def unregister_session(self, session_id: str) -> None:
        """Unregister a session and fully tear it down.

        Cancels the capture task, wakes the per-WS sender tasks, and closes any
        client connections — so a single session unwinds cleanly on its own,
        without waiting for the whole server to stop. This matters when the
        server is shared across concurrent handoffs: one handoff finishing must
        not leave its WebSocket/sender task dangling on the still-running
        server.

        Args:
            session_id: The session to unregister.
        """
        session = self.sessions.pop(session_id, None)
        if session is None:
            return

        if session.capture_task and not session.capture_task.done():
            session.capture_task.cancel()
        # Cancel any in-flight ack/publish tasks (copy first — done callbacks
        # mutate the set as they finish).
        for task in list(session.background_tasks):
            task.cancel()

        async with session.frame_condition:
            session.closed = True
            session.frame_condition.notify_all()
        for ws in list(session.websockets):
            with suppress(Exception):
                await ws.close()

    def is_session_accessed(self, session_id: str) -> bool:
        """Check if a session has been accessed by the user.

        Args:
            session_id: The session to check.

        Returns:
            True if the session has been accessed.
        """
        if session_id in self.sessions:
            return self.sessions[session_id].accessed
        return False

    def get_session(self, session_id: str) -> HandoffSession | None:
        """Get a session by ID.

        Args:
            session_id: The session ID.

        Returns:
            The session, or None if not found.
        """
        return self.sessions.get(session_id)

    @staticmethod
    def _spawn_tracked(
        session: HandoffSession, coro: "Coroutine[Any, Any, Any]"
    ) -> "asyncio.Task[Any]":
        """Schedule a fire-and-forget coroutine while holding a strong ref.

        The event loop keeps only weak references to tasks, so an unreferenced
        ack/publish task could be garbage-collected mid-flight (a dropped ack
        stalls the whole screencast). Keep it in session.background_tasks until
        it completes, then let it remove itself.
        """
        task = asyncio.ensure_future(coro)
        session.background_tasks.add(task)
        task.add_done_callback(session.background_tasks.discard)
        return task

    async def _capture_frames(self, session: HandoffSession) -> None:
        """Capture frames from CDP screencast and publish via Condition."""
        cdp = session.cdp

        def on_frame(params: dict[str, Any]) -> None:
            frame_session_id = params.get("sessionId")
            data = params.get("data", "")

            # Ack first so Chrome keeps producing — otherwise capture stalls.
            if frame_session_id:
                self._spawn_tracked(
                    session,
                    cdp.send("Page.screencastFrameAck", {"sessionId": frame_session_id}),
                )

            if not data:
                return
            try:
                frame_bytes = base64.b64decode(data)
            except Exception:
                return
            # CDP fires this on the loop thread but from a sync callback —
            # schedule the async publish without awaiting it.
            self._spawn_tracked(session, self._publish_frame(session, frame_bytes))

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": self.config.jpeg_quality,
                "maxWidth": session.viewport_size["width"],
                "maxHeight": session.viewport_size["height"],
                "everyNthFrame": self.config.every_nth_frame,
            },
        )

        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            with suppress(Exception):
                await cdp.send("Page.stopScreencast")
            raise

    async def _handle_mouse(self, cdp: "CDPSession", message: dict[str, Any]) -> None:
        """Handle mouse events."""
        action = message.get("action")
        x, y = message.get("x", 0), message.get("y", 0)

        button_map = {0: "left", 1: "middle", 2: "right"}

        if action in ["mousedown", "mouseup"]:
            logger.info(f"Mouse {action} at ({x}, {y})")
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed" if action == "mousedown" else "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": button_map.get(message.get("button", 0), "left"),
                    "clickCount": 1,
                },
            )
        elif action == "mousemove":
            await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        elif action == "wheel":
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": x,
                    "y": y,
                    "deltaX": message.get("deltaX", 0),
                    "deltaY": message.get("deltaY", 0),
                },
            )

    async def _handle_keyboard(self, cdp: "CDPSession", message: dict[str, Any]) -> None:
        """Handle keyboard events with proper special character support."""
        action = message.get("action")
        key = message.get("key", "")
        code = message.get("code", "")
        ctrl = message.get("ctrl", False)
        shift = message.get("shift", False)
        alt = message.get("alt", False)
        meta = message.get("meta", False)

        modifiers = 0
        if alt:
            modifiers |= 1
        if ctrl:
            modifiers |= 2
        if meta:
            modifiers |= 4
        if shift:
            modifiers |= 8

        params: dict[str, Any] = {
            "type": "keyDown" if action == "keydown" else "keyUp",
            "key": key,
            "code": code,
            "modifiers": modifiers,
        }

        # Special keys mapping
        key_codes = {
            "Backspace": 8,
            "Tab": 9,
            "Enter": 13,
            "Escape": 27,
            "Space": 32,
            "PageUp": 33,
            "PageDown": 34,
            "End": 35,
            "Home": 36,
            "ArrowLeft": 37,
            "ArrowUp": 38,
            "ArrowRight": 39,
            "ArrowDown": 40,
            "Delete": 46,
            "F1": 112,
            "F2": 113,
            "F3": 114,
            "F4": 115,
            "F5": 116,
            "F6": 117,
            "F7": 118,
            "F8": 119,
            "F9": 120,
            "F10": 121,
            "F11": 122,
            "F12": 123,
        }

        # Add virtual key code
        if key in key_codes:
            params["windowsVirtualKeyCode"] = key_codes[key]
            params["nativeVirtualKeyCode"] = key_codes[key]
        elif len(key) == 1:
            if key.isalpha():
                key_code = ord(key.upper())
            else:
                key_code = ord(key.upper()) if key.isdigit() else self._get_symbol_keycode(key)
            params["windowsVirtualKeyCode"] = key_code
            params["nativeVirtualKeyCode"] = key_code

        # `text` is what drives the page's default action — inserting the
        # character, submitting a form, adding a newline. It belongs only on
        # keyDown without ctrl/alt/meta (shift is fine). Without it, Enter
        # dispatches a keydown but never submits/inserts. Enter carries a
        # carriage return; other named keys (Tab, arrows, …) carry no text;
        # single printable characters carry themselves.
        if action == "keydown" and not (ctrl or alt or meta):
            if key == "Enter":
                params["text"] = "\r"
            elif len(key) == 1:
                params["text"] = key

        await cdp.send("Input.dispatchKeyEvent", params)

    def _get_symbol_keycode(self, key: str) -> int:
        """Get the Windows virtual key code for symbol characters."""
        symbol_map = {
            "!": 49,
            "@": 50,
            "#": 51,
            "$": 52,
            "%": 53,
            "^": 54,
            "&": 55,
            "*": 56,
            "(": 57,
            ")": 48,
            "1": 49,
            "2": 50,
            "3": 51,
            "4": 52,
            "5": 53,
            "6": 54,
            "7": 55,
            "8": 56,
            "9": 57,
            "0": 48,
            "-": 189,
            "_": 189,
            "=": 187,
            "+": 187,
            "[": 219,
            "{": 219,
            "]": 221,
            "}": 221,
            "\\": 220,
            "|": 220,
            ";": 186,
            ":": 186,
            "'": 222,
            '"': 222,
            ",": 188,
            "<": 188,
            ".": 190,
            ">": 190,
            "/": 191,
            "?": 191,
            "`": 192,
            "~": 192,
        }
        return symbol_map.get(key, ord(key))

    async def _handle_navigate(self, page: "Page", message: dict[str, Any]) -> None:
        """Handle navigation commands."""
        action = message.get("action")
        if action == "reload":
            await page.reload()

    async def notify_task_completed(self, session_id: str, reason: str | None = None) -> None:
        """Notify frontend that task is completed.

        Args:
            session_id: The session that completed.
            reason: Optional completion reason to display.
        """
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.mark_completed(reason or session.reason)
            message = {"type": "task_completed", "reason": reason or session.reason}
            for ws in session.websockets:
                with suppress(Exception):
                    await ws.send_json(message)

    async def stop_screencast(self, session_id: str) -> None:
        """Stop the screencast for a session (e.g., before sensitive data appears).

        Args:
            session_id: The session to stop screencasting.
        """
        if session_id in self.sessions:
            session = self.sessions[session_id]
            if session.capture_task and not session.capture_task.done():
                session.capture_task.cancel()

    def _get_html_client(self, session_id: str, reason: str) -> str:
        """Generate HTML client for streaming using Jinja template."""
        session = self.sessions[session_id]
        template = jinja_env.get_template("intervention.html")
        return template.render(
            session_id=session_id,
            reason=reason,
            viewport_width=session.viewport_size["width"],
            viewport_height=session.viewport_size["height"],
        )

    def get_stream_url(self, session_id: str) -> str:
        """Get the public stream URL for a session.

        Args:
            session_id: The session ID.

        Returns:
            The full URL to access the stream.
        """
        base_url = self.config.get_base_url()
        return f"{base_url}/?session={session_id}"

    async def start(self) -> None:
        """Start the server."""
        config = uvicorn.Config(
            self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    async def stop(self) -> None:
        """Stop the server gracefully.

        Close WebSockets from the server side BEFORE asking uvicorn to exit
        so their handlers unwind via the WebSocketDisconnect path instead
        of getting cancelled mid-await (which surfaces noisy CancelledError
        tracebacks from starlette/uvicorn's protocol layer).
        """
        for session in list(self.sessions.values()):
            # Wake up per-WS sender tasks so they exit cleanly.
            async with session.frame_condition:
                session.closed = True
                session.frame_condition.notify_all()
            for ws in list(session.websockets):
                with suppress(Exception):
                    await ws.close()

        if self._server:
            self._server.should_exit = True
