"""Streaming server for human intervention via CDP screencast."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
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


# Injected into a streamed page to replace native input UIs that the CDP
# screencast can't capture — same fundamental problem as the right-click
# menu above. Native <select> popups, <input type=date|time|...> pickers,
# and color pickers are OS-level overlays rendered outside the page
# viewport, so the operator clicks them and sees nothing happen.
#
# Strategy:
#   * <select> (non-multiple): intercept mousedown, render a DOM overlay
#     of <option>/<optgroup> children. Click an option to set the value
#     and dispatch input + change events so site listeners still fire.
#   * <input type=date|time|datetime-local|month|week>: suppress the
#     native picker, focus the input so the operator can type a value in
#     the input's own native format (browsers accept typed input even
#     when the picker is suppressed).
#
# Out of scope for v1: <select multiple>, <input type=color>,
# <input type=file> (OS file picker is unreachable from page JS).
#
# Globals are non-enumerable so site code that walks window won't see
# them. Listeners run in the capture phase so they fire before site
# handlers; preventDefault on mousedown blocks the native picker, focus()
# is called manually to compensate for the suppressed default focus.
_NATIVE_INPUT_SHIM_JS = """
(() => {
  if (window.__bhInputShim) return;
  Object.defineProperty(window, '__bhInputShim',
    {value: true, enumerable: false, configurable: true});

  const OVERLAY_ID = '__bh_select_overlay';
  const STYLE_ID = '__bh_select_overlay_style';
  const NATIVE_PICKER_TYPES = ['date', 'time', 'datetime-local', 'month', 'week'];

  // Webkit scrollbars inside fixed overlays render very thin (near-invisible
  // until hover). Inline styles can't carry pseudo-elements, so a one-time
  // <style> is the cleanest way to make the scrollbar visible.
  //
  // Inject lazily, not at script-load time: add_init_script runs before
  // document construction, so document.head and document.documentElement
  // are BOTH null on first execution. Doing it the first time we actually
  // open an overlay guarantees the DOM exists.
  function ensureScrollbarStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const host = document.head || document.documentElement;
    if (!host) return;  // truly nothing to attach to — skip silently
    const styleEl = document.createElement('style');
    styleEl.id = STYLE_ID;
    styleEl.textContent =
      '#' + OVERLAY_ID + '::-webkit-scrollbar{width:12px;height:12px}' +
      '#' + OVERLAY_ID + '::-webkit-scrollbar-track{background:#f1f1f1}' +
      '#' + OVERLAY_ID + '::-webkit-scrollbar-thumb' +
      '{background:#b0b0b0;border-radius:6px;border:2px solid #f1f1f1}' +
      '#' + OVERLAY_ID + '::-webkit-scrollbar-thumb:hover{background:#909090}';
    host.appendChild(styleEl);
  }

  let currentOverlay = null;
  let currentSelect = null;

  function closeOverlay() {
    if (currentOverlay) {
      try { currentOverlay.remove(); } catch (e) {}
      currentOverlay = null;
    }
    currentSelect = null;
    window.removeEventListener('resize', closeOverlay, true);
  }

  function selectOption(option) {
    if (!currentSelect || option.disabled) return;
    const select = currentSelect;
    select.value = option.value;
    select.dispatchEvent(new Event('input', {bubbles: true}));
    select.dispatchEvent(new Event('change', {bubbles: true}));
    closeOverlay();
  }

  function openSelectOverlay(select) {
    closeOverlay();
    if (select.disabled || select.multiple) return;
    // Hidden controls have no rendered position to anchor to.
    if (!select.offsetParent && select.offsetHeight === 0) return;
    // Lazy install — DOM is guaranteed to exist by the time the operator
    // can click a select.
    ensureScrollbarStyle();

    const rect = select.getBoundingClientRect();
    const overlay = document.createElement('div');
    overlay.id = OVERLAY_ID;
    overlay.style.cssText = [
      'position:fixed',
      'background:#fff',
      'border:1px solid #999',
      'box-shadow:0 4px 12px rgba(0,0,0,.18)',
      'font:14px system-ui,-apple-system,sans-serif',
      'color:#111',
      'border-radius:4px',
      'max-height:280px',
      'overflow-y:auto',
      'z-index:2147483647',
      'min-width:' + rect.width + 'px',
      'left:' + rect.left + 'px',
    ].join(';');

    // Place below by default; flip above if there's not enough room and
    // more space exists upward.
    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top;
    if (spaceBelow < 200 && spaceAbove > spaceBelow) {
      overlay.style.bottom = (window.innerHeight - rect.top) + 'px';
    } else {
      overlay.style.top = rect.bottom + 'px';
    }

    let selectedRow = null;

    function appendNode(node, indent) {
      if (node.tagName === 'OPTGROUP') {
        const label = document.createElement('div');
        label.textContent = node.label || '';
        label.style.cssText =
          'padding:6px 12px;font-weight:600;color:#666;background:#f3f3f3';
        overlay.appendChild(label);
        for (const child of node.children) appendNode(child, indent + 1);
      } else if (node.tagName === 'OPTION') {
        const row = document.createElement('div');
        row.textContent = node.label || node.textContent;
        const padLeft = 12 + indent * 12;
        row.style.cssText = [
          'padding:6px 12px 6px ' + padLeft + 'px',
          'cursor:' + (node.disabled ? 'not-allowed' : 'pointer'),
          'color:' + (node.disabled ? '#aaa' : '#111'),
          'user-select:none',
        ].join(';');
        if (node.selected) {
          row.style.background = '#e6f0ff';
          selectedRow = row;
        }
        if (!node.disabled) {
          row.addEventListener('mouseenter', () => {
            row.style.background = '#e6f0ff';
          });
          row.addEventListener('mouseleave', () => {
            row.style.background = node.selected ? '#e6f0ff' : '';
          });
          // mousedown (not click) so we beat any focusout/blur that would
          // close the overlay before the click registers.
          row.addEventListener('mousedown', (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            selectOption(node);
          });
        }
        overlay.appendChild(row);
      }
    }
    for (const child of select.children) appendNode(child, 0);

    currentOverlay = overlay;
    currentSelect = select;
    document.body.appendChild(overlay);

    // Manually scroll the overlay on wheel and swallow the event before
    // anyone else can see it. Without this, the page's own wheel listeners
    // (parallax scripts, modal scroll-lock libs) preventDefault on every
    // wheel they see and the operator can't scroll the option list. The
    // page-level scroll-close handler also reads window.scroll, which
    // shouldn't fire here because we scroll the element, not the window.
    overlay.addEventListener(
      'wheel',
      (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        overlay.scrollTop += ev.deltaY;
      },
      {passive: false, capture: true}
    );

    // position:fixed pins the overlay to the viewport, so a page scroll
    // doesn't actually displace it — no need to close on scroll. Resize
    // does displace it (the anchored left/top become wrong relative to
    // the moved select), so resize-close stays.
    window.addEventListener('resize', closeOverlay, true);

    if (selectedRow) {
      try { selectedRow.scrollIntoView({block: 'nearest'}); } catch (e) {}
    }
  }

  document.addEventListener('mousedown', (e) => {
    const t = e.target;
    if (!t) return;

    if (t.tagName === 'SELECT' && !t.multiple && !t.disabled) {
      // Don't focus() the select — focus triggers the browser's
      // "scroll focused element into view" which fires a window scroll
      // and trips any open-overlay teardown the page might do. The
      // overlay is the actual UI now; the select doesn't need focus.
      e.preventDefault();
      if (currentSelect === t) {
        closeOverlay();  // toggle
      } else {
        openSelectOverlay(t);
      }
      return;
    }

    if (
      t.tagName === 'INPUT' &&
      NATIVE_PICKER_TYPES.indexOf(t.type) !== -1 &&
      !t.disabled &&
      !t.readOnly
    ) {
      // Suppress the native picker, focus the input so the operator can
      // type. Browsers still accept typed values for these types.
      e.preventDefault();
      try { t.focus(); } catch (err) {}
      return;
    }

    // Click outside an open overlay closes it. Clicks inside the overlay
    // bubble through; the option row's own mousedown handler picks them up.
    if (currentOverlay && !currentOverlay.contains(t)) {
      closeOverlay();
    }
  }, true);

  document.addEventListener('keydown', (e) => {
    if (currentOverlay && e.key === 'Escape') {
      e.preventDefault();
      closeOverlay();
    }
  }, true);
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
        # Public capability token -> internal session id. The stream endpoints
        # resolve by token (the secret in the URL); session_id stays internal.
        self._token_to_session: dict[str, str] = {}
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
            self._token_to_session.clear()

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
        async def index(t: str | None = None):
            """Serve the HTML client. `t` is the capability token."""
            session_state = self._resolve_token(t)
            if not session_state:
                return HTMLResponse("<h1>Session not found</h1>", status_code=404)

            # Mark session as accessed
            session_state.mark_accessed()

            return self._get_html_client(session_state.session_id, session_state.reason)

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket, t: str | None = None):
            """WebSocket endpoint: binary frames out, JSON control in.

            `t` is the capability token; an unknown or expired one is closed.
            """
            await websocket.accept()

            session_state = self._resolve_token(t)
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

            # Seed the URL bar so it isn't blank until the next navigation.
            if session_state.current_url is not None:
                with suppress(Exception):
                    await websocket.send_json(
                        {"type": "url_changed", "url": session_state.current_url}
                    )

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
                            # Bumping operator_activity on each routed event
                            # is what lets detections (LLMDetection today)
                            # gate work on operator presence + idleness
                            # instead of page activity. Skipped for ping,
                            # copy_request, cut_request — passive reads, not
                            # real interaction.
                            if msg_type == "mouse":
                                await self._handle_mouse(cdp, message)
                                session_state.operator_activity.bump()
                            elif msg_type == "keyboard":
                                await self._handle_keyboard(cdp, message)
                                session_state.operator_activity.bump()
                            elif msg_type == "navigate":
                                await self._handle_navigate(page, message)
                                session_state.operator_activity.bump()
                            elif msg_type == "paste":
                                await self._handle_paste(cdp, message)
                                session_state.operator_activity.bump()
                            elif msg_type == "copy_request":
                                text = await self._read_selection(page)
                                with suppress(Exception):
                                    await websocket.send_json(
                                        {"type": "copy_response", "text": text}
                                    )
                            elif msg_type == "cut_request":
                                # Read the selection, ship it back as the
                                # copy payload, then dispatch Delete to
                                # remove it. Only effective in editable
                                # contexts (input/textarea/contenteditable)
                                # — same behavior as native ctrl+x.
                                text = await self._read_selection(page)
                                with suppress(Exception):
                                    await websocket.send_json(
                                        {"type": "copy_response", "text": text, "cut": True}
                                    )
                                if text:
                                    with suppress(Exception):
                                        await cdp.send("Input.dispatchKeyEvent", {
                                            "type": "keyDown",
                                            "key": "Delete",
                                            "code": "Delete",
                                            "windowsVirtualKeyCode": 46,
                                            "nativeVirtualKeyCode": 46,
                                        })
                                        await cdp.send("Input.dispatchKeyEvent", {
                                            "type": "keyUp",
                                            "key": "Delete",
                                            "code": "Delete",
                                            "windowsVirtualKeyCode": 46,
                                            "nativeVirtualKeyCode": 46,
                                        })
                            elif msg_type == "ping":
                                # Echo the client's timestamp straight back so
                                # the viewer can measure RTT against its own
                                # clock. Avoids any server/client clock skew.
                                with suppress(Exception):
                                    await websocket.send_json(
                                        {"type": "pong", "ts": message.get("ts")}
                                    )
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
        scenario_name: str | None = None,
        viewport_size: dict[str, int] | None = None,
        stream_url: str | None = None,
        crop_metrics: dict[str, int] | None = None,
    ) -> HandoffSession:
        """Register a new Page for streaming.

        Args:
            session_id: Unique identifier for the session.
            page: Playwright page to stream.
            context: Browser context the page belongs to.
            reason: Reason for handoff (shown to user).
            scenario_name: Label of the matched scenario (breadcrumb header).
            viewport_size: Optional viewport dimensions.
            stream_url: Optional substrate viewer URL. When set, the session
                runs in passthrough mode: the screencast pump is skipped and
                the operator gets a wrapper page that iframes this URL.
            crop_metrics: Page-rect-on-display metrics for the iframe crop.
                Six ints (screen_w/h, page_x/y, page_w/h) captured via
                page.evaluate at handoff start. Only meaningful with stream_url.

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
            scenario_name=scenario_name,
            viewport_size=viewport_size or DEFAULT_VIEWPORT.copy(),
            stream_url=stream_url,
            crop_metrics=crop_metrics,
        )
        # The token can't outlive the handoff itself, which is bounded by
        # session_timeout — so a leaked link is dead once the handoff ends.
        session.expires_at = time.time() + self.config.session_timeout
        self.sessions[session_id] = session
        self._token_to_session[session.access_token] = session_id

        # Page-modifying helpers (context-menu suppression, native input shim)
        # exist to make the operator's stream view behave like a real browser
        # for input. In passthrough mode the operator interacts via the
        # substrate's own viewer — touching the page from here would be a
        # no-op at best and a behavior change at worst. Skip.
        if not session.is_passthrough:
            await self._suppress_context_menu(page)
            await self._inject_native_input_shim(page)

        # Seed the URL with whatever Playwright has now, then track every
        # subsequent main-frame navigation so the viewer's URL bar matches
        # the page. Subframe navigations are ignored — operators care about
        # the document URL, not third-party iframes. Both modes need this —
        # the proxy template also renders a URL bar.
        with suppress(Exception):
            session.current_url = page.url
        self._attach_url_tracker(session)

        # The initial screenshot + screencast pump only exist to feed the
        # streaming WS. In passthrough mode there's no streaming WS to feed
        # — frames flow operator <-> substrate directly via whatever
        # transport the substrate's viewer uses.
        if not session.is_passthrough:
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

    def _attach_url_tracker(self, session: HandoffSession) -> None:
        """Push main-frame URL changes to every connected viewer.

        Playwright fires framenavigated on the loop thread but from a sync
        callback, so the async fan-out has to be scheduled via the
        background-task pattern. Subframes (ads, embeds) are filtered out
        — the URL bar is for the document.
        """
        page = session.page

        def on_framenavigated(frame: Any) -> None:
            if frame != page.main_frame:
                return
            try:
                url = frame.url
            except Exception:
                return
            session.current_url = url
            payload = {"type": "url_changed", "url": url}
            for ws in list(session.websockets):
                self._spawn_tracked(session, self._safe_send_json(ws, payload))

        page.on("framenavigated", on_framenavigated)

    @staticmethod
    async def _safe_send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
        """send_json that swallows transport errors.

        A disconnected viewer must not abort a fan-out: the same payload is
        being delivered to N other viewers concurrently.
        """
        with suppress(Exception):
            await websocket.send_json(payload)

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
    async def _inject_native_input_shim(page: "Page") -> None:
        """Replace native <select> popups and date/time pickers with DOM UI.

        Same install pattern as the context-menu guard: add_init_script for
        future documents, evaluate for the already-loaded one. Both
        best-effort — a transient failure must not abort the handoff, the
        operator can still drive the page via keyboard / type-to-search even
        if the shim doesn't install.
        """
        with suppress(Exception):
            await page.add_init_script(_NATIVE_INPUT_SHIM_JS)
        with suppress(Exception):
            await page.evaluate(_NATIVE_INPUT_SHIM_JS)

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
        # Drop the token so a leaked link stops resolving the moment the
        # handoff ends.
        self._token_to_session.pop(session.access_token, None)

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

    def _resolve_token(self, token: str | None) -> HandoffSession | None:
        """Resolve a capability token to its session, or None.

        Returns None for a missing/unknown token or one past its expiry — the
        endpoints treat all three identically (not found), so a caller can't
        distinguish "wrong token" from "expired".
        """
        if not token:
            return None
        session_id = self._token_to_session.get(token)
        if session_id is None:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at is not None and time.time() > session.expires_at:
            return None
        return session

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
        """Capture frames from CDP screencast and publish via Condition.

        Defensive early return for passthrough sessions: register_session
        already skips scheduling this task in that mode, but if a caller
        invokes _capture_frames directly we must not start a screencast on
        a session whose viewer is the substrate's (not ours).
        """
        if session.is_passthrough:
            return
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
            # clickCount drives the remote's double/triple-click detection
            # (word/paragraph selection). The client forwards `e.detail` from
            # the local MouseEvent, which is the consecutive-click count.
            click_count = int(message.get("clickCount", 1) or 1)
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed" if action == "mousedown" else "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": button_map.get(message.get("button", 0), "left"),
                    "clickCount": click_count,
                },
            )
        elif action == "mousemove":
            # CDP needs the held-button identity on mouseMoved for drag
            # semantics — without it, a move during a held click is treated
            # as a hover and the remote never extends a text selection.
            buttons = int(message.get("buttons", 0) or 0)
            params: dict[str, Any] = {"type": "mouseMoved", "x": x, "y": y, "buttons": buttons}
            if buttons & 1:
                params["button"] = "left"
            elif buttons & 2:
                params["button"] = "right"
            elif buttons & 4:
                params["button"] = "middle"
            await cdp.send("Input.dispatchMouseEvent", params)
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

    @staticmethod
    async def _handle_paste(cdp: "CDPSession", message: dict[str, Any]) -> None:
        """Insert clipboard text from the operator at the page's focus.

        The remote browser has its own clipboard, isolated from the operator's,
        so `Input.insertText` is used instead of dispatching ctrl+v — the
        operator's local clipboard is the authority and the remote just drops
        the text where the caret is.
        """
        text = message.get("text", "")
        if not text:
            return
        await cdp.send("Input.insertText", {"text": text})

    @staticmethod
    async def _read_selection(page: "Page") -> str:
        """Return the current text selection on the remote page.

        Empty string when nothing is selected — used as the copy payload sent
        back to the operator's clipboard.
        """
        try:
            return await page.evaluate("() => window.getSelection().toString()")
        except Exception:
            return ""

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
            access_token=session.access_token,
            reason=reason,
            scenario_name=session.scenario_name,
            viewport_width=session.viewport_size["width"],
            viewport_height=session.viewport_size["height"],
        )

    def get_operator_url(self, session_id: str) -> str:
        """Get the public operator URL for a session.

        The URL carries the session's capability token (not the session id),
        which is the secret an operator needs to open the wrapper page.
        The Handoff sends this URL to the operator via notifiers; the
        operator's browser loads it and gets the intervention or proxy
        template depending on session mode.

        Args:
            session_id: The session ID.

        Returns:
            The full URL the operator opens.
        """
        base_url = self.config.get_base_url()
        token = self.sessions[session_id].access_token
        return f"{base_url}/?t={token}"

    def get_stream_url(self, session_id: str) -> str:
        """Deprecated alias for :meth:`get_operator_url`. Removed in v0.6."""
        import warnings

        warnings.warn(
            "get_stream_url() is deprecated; use get_operator_url() instead. "
            "Will be removed in v0.6.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.get_operator_url(session_id)

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
