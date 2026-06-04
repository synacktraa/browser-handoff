"""Operator activity signal shared between the streaming server and detections.

Detections that gate expensive work on operator presence (currently
LLMDetection — vision calls are not free) need a way to ask two questions
that the page itself can't answer:

  * Has any operator interacted with this handoff yet?
  * When did the operator last interact?

The streaming server is the source of truth for both — it sees every
mouse/keyboard/paste/navigate event the operator forwards over the WebSocket.
OperatorActivity is the small object the server hangs off each HandoffSession
so a bound detection can read the answer without coupling to the server.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class OperatorActivity:
    """Per-handoff record of operator interaction with the streamed page.

    Owned by HandoffSession; mutated by StreamingServer on each routed
    operator input event; read by detections that opt into operator-driven
    gating via BaseDetection.bind().

    `last_activity` is a time.monotonic() timestamp (NOT wall-clock) so it
    composes cleanly with idle/interval math in `_should_check` without
    being affected by NTP jumps. None means "no operator has touched this
    session yet" — the load-bearing signal for LLMDetection's "don't burn
    vision calls before anyone is here" rule.

    `_first_interaction` is a set-once Event so detections can block on
    `wait_for_first_interaction()` instead of polling. Never cleared — once
    an operator has interacted, the gate stays open for the rest of the
    handoff's life.
    """

    last_activity: float | None = None
    _first_interaction: asyncio.Event = field(default_factory=asyncio.Event)

    def bump(self) -> None:
        """Record an operator interaction at the current monotonic time."""
        self.last_activity = time.monotonic()
        self._first_interaction.set()

    async def wait_for_first_interaction(self) -> None:
        """Block until the operator has interacted at least once.

        Returns immediately if bump() has already been called.
        """
        await self._first_interaction.wait()

    @property
    def has_ever_interacted(self) -> bool:
        """True once the operator has produced at least one input event."""
        return self.last_activity is not None
