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

# Default viewport size
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class SessionPresence:
    """Operator presence on a handoff session.

    One primitive answers the two questions Handoff.wait_for_completion
    needs to gate on:

      * Has any operator opened the wrapper page yet? (`wait_until_connected`)
      * Are they still there right now, or did they close the tab? (`state`)

    Both signals are produced by the same wrapper-side heartbeat — a
    `presence` message sent every ~2 seconds while the tab is visible. The
    WS handler calls `bump()` on each one (and on first accept, implicitly).
    Coalescing both signals into one primitive means callers don't have to
    keep two events in sync — the first bump both records the timestamp
    and flips the one-shot connect event.

    `last_ping_ts` is a time.monotonic() timestamp (not wall-clock) so
    freshness comparisons are immune to NTP jumps. The internal
    `_connected` event is non-public on purpose — outside callers go
    through `wait_until_connected()` so the contract stays "presence
    primitive owns its own state machine."
    """

    last_ping_ts: float | None = None
    # Wrapper sends presence every 2s while visible; 5s gives 2× cadence
    # plus slack for transport jitter without making the gate sticky.
    freshness_threshold: float = 5.0
    _connected: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def bump(self) -> None:
        """Record a presence ping and flip the connected gate on first call.

        Idempotent on `_connected` (Event.set is itself idempotent, but
        making the intent explicit reads cleaner). The first bump in a
        session's life is the wrapper accepting the WS — that's also the
        moment Handoff.wait_for_completion is allowed to install the
        detection's listeners.
        """
        self.last_ping_ts = time.monotonic()
        if not self._connected.is_set():
            self._connected.set()

    @property
    def state(self) -> Literal["inactive", "present", "stale"]:
        """Current presence state for the orchestration gate.

        - "inactive": no wrapper has accepted the WS yet (no bump ever).
        - "present":  a presence ping arrived within freshness_threshold.
        - "stale":    wrapper connected once but pings dried up
                      (operator closed the tab / backgrounded it / lost
                      network).

        Orchestration treats only "present" as "operator is here right
        now, run the detection check." The three-state split matches the
        vocabulary the wrapper UI uses client-side.
        """
        if self.last_ping_ts is None:
            return "inactive"
        if (time.monotonic() - self.last_ping_ts) <= self.freshness_threshold:
            return "present"
        return "stale"

    async def wait_until_connected(self) -> None:
        """Block until the first wrapper has connected (first `bump()`).

        Returns immediately once any bump has happened. Used by
        Handoff.wait_for_completion as the lazy-install gate — detection
        listeners only register after the operator has authenticated via
        the wrapper token, which closes the substrate-URL leak.
        """
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
    # Last URL Playwright reported for the page's main frame. Updated on
    # every main-frame navigation and pushed to all connected viewers so
    # the URL bar reflects where the page actually is.
    current_url: str | None = None
    # The capability secret in the stream URL. ~256-bit, CSPRNG, decoupled from
    # session_id (which is the correlation key in logs) so the secret never
    # lands in general logging. The streaming endpoints resolve by this, not by
    # session_id.
    access_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # Wall-clock deadline after which the token stops resolving, even if the
    # session lingers. Set by the server at registration to bound a leaked link.
    expires_at: float | None = None
    viewport_size: dict[str, int] = field(default_factory=lambda: DEFAULT_VIEWPORT.copy())
    capture_task: asyncio.Task[None] | None = None
    # Strong references to fire-and-forget tasks (per-frame ack + publish).
    # The event loop only keeps weak references, so a task not held here could
    # be garbage-collected mid-execution — dropping a frame or, worse, a
    # screencast ack (which would stall capture). Each task removes itself on
    # completion via add_done_callback.
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    accessed: bool = False
    latest_frame: bytes | None = None
    frame_seq: int = 0
    frame_condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    closed: bool = False
    websockets: list["WebSocket"] = field(default_factory=list)
    completed: bool = False
    completion_reason: str | None = None
    # Single source of truth for "has an operator opened the wrapper page
    # and are they still there." Bumped by the WS handler on each accept
    # and each `presence` message from the wrapper. Orchestration awaits
    # `wait_until_connected()` before installing detection listeners (lazy
    # install — closes the substrate-URL leak) and reads `state` in the
    # completion callback so a detection scheduled to fire while the
    # operator has wandered off doesn't burn the LLM call.
    presence: SessionPresence = field(default_factory=SessionPresence)
    # Passthrough mode: when set, browser-handoff skips its own CDP screencast
    # and instead embeds the substrate's own viewer URL inside a thin wrapper
    # template. browser-handoff keeps the detection + notification + lifecycle
    # responsibilities; the substrate handles frames and operator input via
    # whatever transport it ships (WebRTC, noVNC, etc.).
    stream_url: str | None = None
    # Page rect on the substrate's display, captured once at handoff start via
    # window.screen + window.screenX/Y + (outerH - innerH) chrome offset. The
    # proxy template's CSS uses these six numbers to crop the iframe to just
    # the page content. None when not in passthrough mode or when the JS
    # evaluate returned degenerate values (e.g. headless mocks).
    crop_metrics: dict[str, int] | None = None

    @property
    def is_passthrough(self) -> bool:
        """True iff this session delegates streaming to an external viewer.

        Derived from `stream_url`; there is no separate mode field so the
        two can't disagree.
        """
        return self.stream_url is not None

    def mark_accessed(self) -> None:
        """Mark the session as accessed by a user."""
        self.accessed = True

    def mark_completed(self, reason: str) -> None:
        """Mark the session as completed."""
        self.completed = True
        self.completion_reason = reason
