"""Streaming server for human intervention via CDP screencast."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader

from .config import ServerConfig
from .session import DEFAULT_VIEWPORT, HandoffSession

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, CDPSession, Page

# Setup Jinja2 templates
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)


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
            reason="Cloudflare challenge detected",
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
            self.sessions.clear()

        app = FastAPI(title="Browser Handoff Stream", lifespan=lifespan)

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/", response_class=HTMLResponse)
        async def index(session: str = "default") -> str | HTMLResponse:
            """Serve the HTML client."""
            session_state = self.sessions.get(session)
            if not session_state:
                return HTMLResponse("<h1>Session not found</h1>", status_code=404)

            # Mark session as accessed
            session_state.mark_accessed()

            return self._get_html_client(session, session_state.reason)

        @app.get("/stream")
        async def stream(session: str = "default") -> HTMLResponse | StreamingResponse:
            """MJPEG stream endpoint."""
            session_state = self.sessions.get(session)
            if not session_state:
                return HTMLResponse("Session not found", status_code=404)

            async def generate():
                try:
                    while True:
                        frame_data = await asyncio.wait_for(
                            session_state.frame_queue.get(), timeout=120.0
                        )
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + frame_data + b"\r\n"
                        )
                except TimeoutError:
                    pass

            return StreamingResponse(
                generate(), media_type="multipart/x-mixed-replace; boundary=frame"
            )

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket, session: str = "default"):
            """WebSocket endpoint for control commands."""
            await websocket.accept()

            session_state = self.sessions.get(session)
            if not session_state:
                await websocket.close()
                return

            session_state.websockets.append(websocket)
            cdp = session_state.cdp
            page = session_state.page

            try:
                with suppress(WebSocketDisconnect):
                    while True:
                        data = await websocket.receive_text()
                        message = json.loads(data)
                        msg_type = message.get("type")

                        if msg_type == "mouse":
                            await self._handle_mouse(cdp, message)
                        elif msg_type == "keyboard":
                            await self._handle_keyboard(cdp, message)
                        elif msg_type == "navigate":
                            await self._handle_navigate(page, message)
            except Exception:
                pass
            finally:
                if websocket in session_state.websockets:
                    session_state.websockets.remove(websocket)

        return app

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

        # Take initial screenshot as first frame
        with suppress(Exception):
            screenshot_bytes = await page.screenshot(type="jpeg", quality=85)
            session.latest_frame = screenshot_bytes
            session.frame_queue.put_nowait(screenshot_bytes)

        # Start capture immediately
        session.capture_task = asyncio.create_task(self._capture_frames(session))

        return session

    async def unregister_session(self, session_id: str) -> None:
        """Unregister a session.

        Args:
            session_id: The session to unregister.
        """
        if session_id in self.sessions:
            session = self.sessions[session_id]
            if session.capture_task and not session.capture_task.done():
                session.capture_task.cancel()
            del self.sessions[session_id]

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

    async def _capture_frames(self, session: HandoffSession) -> None:
        """Capture frames from CDP screencast."""
        cdp = session.cdp

        def on_frame(params: dict[str, Any]) -> None:
            frame_session_id = params.get("sessionId")
            data = params.get("data", "")

            if frame_session_id:
                asyncio.create_task(
                    cdp.send("Page.screencastFrameAck", {"sessionId": frame_session_id})
                )

            if data:
                with suppress(Exception):
                    frame_bytes = base64.b64decode(data)
                    session.latest_frame = frame_bytes
                    if session.frame_queue.full():
                        session.frame_queue.get_nowait()
                    session.frame_queue.put_nowait(frame_bytes)

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 85,
                "maxWidth": session.viewport_size["width"],
                "maxHeight": session.viewport_size["height"],
                "everyNthFrame": 1,
            },
        )

        try:
            while True:
                await asyncio.sleep(1)
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

        # Add text only for keyDown of single printable characters (no ctrl/alt/meta modifiers)
        if action == "keydown" and len(key) == 1 and not (ctrl or alt or meta):
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
        """Stop the server."""
        if self._server:
            self._server.should_exit = True
