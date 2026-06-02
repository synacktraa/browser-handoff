"""Session management for streaming server."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import WebSocket
    from playwright.async_api import BrowserContext, CDPSession, Page

# Default viewport size
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


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

    def mark_accessed(self) -> None:
        """Mark the session as accessed by a user."""
        self.accessed = True

    def mark_completed(self, reason: str) -> None:
        """Mark the session as completed."""
        self.completed = True
        self.completion_reason = reason
