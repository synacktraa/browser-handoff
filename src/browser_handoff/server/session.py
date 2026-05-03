"""Session management for streaming server."""

from __future__ import annotations

import asyncio
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
    viewport_size: dict[str, int] = field(default_factory=lambda: DEFAULT_VIEWPORT.copy())
    frame_queue: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=3))
    capture_task: asyncio.Task[None] | None = None
    accessed: bool = False
    latest_frame: bytes | None = None
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
