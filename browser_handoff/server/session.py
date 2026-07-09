"""Session management for streaming server."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from fastapi import WebSocket
    from playwright.async_api import BrowserContext, CDPSession, Page

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class SessionPresence:
    """Whether an operator's wrapper is currently attached to the session.

    The WS handler calls `bump()` on first accept and on each `presence`
    message from the wrapper (~every 2s while visible). The first bump
    both records the timestamp and flips the one-shot connect gate, so
    `bump()` is the single mutation point — callers don't need to
    coordinate two fields.

    `last_ping_ts` uses time.monotonic() so freshness math is immune to
    NTP jumps. `_connected` is private; outside callers go through
    `wait_until_connected()`.
    """

    last_ping_ts: float | None = None
    # Wrapper pings every 2s; 5s gives 2× cadence plus jitter slack.
    freshness_threshold: float = 5.0
    _connected: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def bump(self) -> None:
        """Record a presence ping; flip the connect gate on first call."""
        self.last_ping_ts = time.monotonic()
        if not self._connected.is_set():
            self._connected.set()

    @property
    def state(self) -> Literal["inactive", "present", "stale"]:
        """Current presence state.

        - "inactive": no wrapper has connected yet (no bump ever).
        - "present":  a ping arrived within `freshness_threshold`.
        - "stale":    wrapper connected once but pings dried up.

        Orchestration runs a check only on "present".
        """
        if self.last_ping_ts is None:
            return "inactive"
        if (time.monotonic() - self.last_ping_ts) <= self.freshness_threshold:
            return "present"
        return "stale"

    async def wait_until_connected(self) -> None:
        """Block until the first `bump()` lands; return immediately after."""
        await self._connected.wait()


@dataclass
class HandoffSession:
    """State for a streaming session."""

    session_id: str
    page: "Page"
    context: "BrowserContext"
    cdp: "CDPSession"
    reason: str
    # Label of the scenario that triggered this handoff. Rendered as the
    # middle segment of the stream viewer's breadcrumb header so the
    # operator knows which trigger fired.
    scenario_name: str | None = None
    # Last URL Playwright reported for the main frame; pushed to viewers
    # so the operator's URL bar reflects the real page.
    current_url: str | None = None
    # ~256-bit CSPRNG secret in the stream URL. Decoupled from
    # session_id (the log-correlation key) so the secret never lands in
    # general logging. Streaming endpoints resolve by this, not session_id.
    access_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # Wall-clock deadline after which the token stops resolving — bounds
    # a leaked link even if the session lingers.
    expires_at: float | None = None
    viewport_size: dict[str, int] = field(default_factory=lambda: DEFAULT_VIEWPORT.copy())
    capture_task: asyncio.Task[None] | None = None
    # Strong refs to fire-and-forget tasks (per-frame ack + publish).
    # The event loop only keeps weak refs, so an un-held task can be
    # GC'd mid-flight — dropping a frame or, worse, a screencast ack
    # (which would stall capture). Each task self-removes on completion.
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    accessed: bool = False
    latest_frame: bytes | None = None
    frame_seq: int = 0
    frame_condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    closed: bool = False
    websockets: list["WebSocket"] = field(default_factory=list)
    completed: bool = False
    completion_reason: str | None = None
    # Wrapper-presence signal. Bumped by the WS handler on accept and on
    # each `presence` message; orchestration awaits
    # `wait_until_connected()` for lazy install and reads `state` to gate
    # detection checks on "operator is here right now."
    presence: SessionPresence = field(default_factory=SessionPresence)
    # Passthrough mode: when set, browser-handoff skips its own CDP
    # screencast and the operator wrapper iframes this substrate URL.
    # bh still owns detection + notification + lifecycle.
    stream_url: str | None = None
    # Page rect on the substrate's display (six ints: screen_w/h,
    # page_x/y, page_w/h). Used by the passthrough template's CSS to crop
    # the iframe to just the page content. None when not in passthrough mode
    # or when the JS evaluate returned degenerate values.
    crop_metrics: dict[str, int] | None = None
    # Per-session resolved timeouts. None at either layer means "no
    # bound at this layer." access_timeout fires before first connect;
    # completion_timeout fires after.
    access_timeout: float | None = None
    completion_timeout: float | None = None
    # Set by the access-deadline task right before it returns. The WS
    # upgrade handler reads this to reject late operator clicks with
    # 1008. First-connect-gated, not wall-clock: a connect-then-drop
    # before access_timeout retires the timer and leaves this False.
    access_timer_fired: bool = False
    # Wall-clock epoch (time.time) at which completion_timeout fires.
    # Anchored on first WS connect so reconnects see the same deadline;
    # the wrapper's countdown banner reads this via the session_state
    # WS message. None when completion_timeout is None or no connect yet.
    completion_deadline_ts: float | None = None

    @property
    def is_passthrough(self) -> bool:
        """True iff this session delegates streaming to a substrate viewer."""
        return self.stream_url is not None

    def mark_accessed(self) -> None:
        self.accessed = True

    def mark_completed(self, reason: str) -> None:
        self.completed = True
        self.completion_reason = reason
