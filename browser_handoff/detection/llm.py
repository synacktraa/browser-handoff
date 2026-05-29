"""LLM-based detection using vision models."""

from __future__ import annotations

import asyncio
import base64
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from .base import BaseDetection, DetectionResult

if TYPE_CHECKING:
    from playwright.async_api import Page

# System prompt for LLM detection
SYSTEM_PROMPT = """You are analyzing a browser screenshot to determine if a condition is met.
Respond with only "yes" or "no"."""

USER_PROMPT_TEMPLATE = """Based on this screenshot, is the following condition true?

Condition: {condition}

Answer only "yes" or "no"."""

def _activity_setup_js(var: str) -> str:
    """JS injected once per document: passive listeners that stamp a hidden
    window property with Date.now() on any human input or DOM mutation. The
    watch loop polls that stamp (a cheap number read) and only spends a vision
    call when it advances, so checks track real page activity.

    `var` is a per-session random property name, defined non-enumerable, so the
    instrumentation stays off the page's observable surface: site JS can't
    enumerate it (Object.keys / for-in / JSON.stringify skip it) and can't
    guess the name to probe for it. mousemove is deliberately excluded — it
    fires continuously as the cursor moves and means nothing for task progress.
    """
    return (
        "(() => {"
        f"if (window.{var} !== undefined) return;"
        f"Object.defineProperty(window, '{var}', "
        "{value: 0, writable: true, enumerable: false, configurable: true});"
        f"const mark = () => {{ window.{var} = Date.now(); }};"
        "['pointerdown','keydown','input','change','scroll','wheel']"
        ".forEach((ev) => window.addEventListener(ev, mark, true));"
        "try {"
        "const mo = new MutationObserver(mark);"
        "mo.observe(document, "
        "{subtree:true, childList:true, attributes:true, characterData:true});"
        "} catch (e) {}"
        "})();"
    )


def _activity_read_js(var: str) -> str:
    """JS that returns the latest activity stamp (0 if none yet)."""
    return f"() => window.{var} || 0"


@dataclass
class LLMDetection(BaseDetection):
    """Detection based on LLM (vision) analysis of screenshots.

    check() takes one screenshot and asks the model (via LiteLLM) whether the
    condition holds. Because that vision call is the expensive part, watching
    (register_listeners) is activity-gated rather than timed: it tracks human
    input, DOM mutations, and navigation, and runs a single check once activity
    *settles* for `idle_seconds`, plus a safety-net poll every `max_interval`
    seconds to catch changes it can't observe (e.g. canvas/video).

    Example:
        detection = LLMDetection(
            model="anthropic/claude-sonnet-4-5",
            condition="The page is showing a login form or asking for credentials",
            api_key="sk-ant-...",   # optional; falls back to provider env var
            idle_seconds=2.0,       # check after the page is quiet this long
            max_interval=30.0,      # safety-net poll while a handoff is active
        )
    """

    detection_type: str = field(default="llm", init=False)

    model: str = "anthropic/claude-sonnet-4-5"
    condition: str = ""
    # If None, litellm picks up the key from the provider's env var
    # (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, ...).
    api_key: str | None = None

    # Debounce window: run a check once the page has been quiet this long after
    # activity. The dominant cost knob — bigger means fewer, later checks.
    idle_seconds: float = 2.0
    # Safety-net: check at least this often once there has been any activity,
    # even with nothing new observed. 0 disables it (debounce-only).
    max_interval: float = 30.0

    # How often the loop polls the JS activity stamp. Cheap (reads a number),
    # so this is small; not a public knob.
    _poll_interval: float = field(default=0.5, init=False, repr=False)

    @staticmethod
    def _should_check(
        now: float,
        last_activity: float | None,
        last_check: float,
        last_check_activity: float | None,
        idle_seconds: float,
        max_interval: float,
    ) -> bool:
        """Decide whether to run a vision check this tick.

        Pure function of the timing state so it can be tested without a browser
        or an LLM call. A check fires when EITHER:
          - activity has settled: the most recent activity is newer than what
            the last check covered, and the page has since been quiet for
            idle_seconds; OR
          - the safety net trips: max_interval has elapsed since the last check
            (and there has been activity at all).
        Never fires before the first activity.
        """
        if last_activity is None:
            return False
        new_activity = last_check_activity is None or last_activity > last_check_activity
        settled = new_activity and (now - last_activity) >= idle_seconds
        stale = max_interval > 0 and (now - last_check) >= max_interval
        return settled or stale

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Watch for activity and run a debounced check (see _should_check)."""
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        # Per-session, non-enumerable property name so the injected activity
        # stamp can't be enumerated or probed for by the page (see
        # _activity_setup_js).
        activity_var = f"__bh_{secrets.token_hex(8)}"
        setup_js = _activity_setup_js(activity_var)
        read_js = _activity_read_js(activity_var)
        state: dict[str, Any] = {
            "last_activity": None,       # loop.time() of most recent activity
            "last_check": loop.time(),   # loop.time() of most recent check
            "last_check_activity": None, # last_activity value the last check covered
            "prev_ms": 0,                # last JS activity stamp we've seen
        }

        def mark_activity(*_args: Any) -> None:
            state["last_activity"] = loop.time()

        # Navigation is activity, and fires synchronously — register it now.
        page.on("framenavigated", mark_activity)

        async def watch() -> None:
            # Install on every future document (re-runs on navigation) and on
            # the one already loaded.
            with suppress(Exception):
                await page.add_init_script(setup_js)
            with suppress(Exception):
                await page.evaluate(setup_js)

            while not stop_event.is_set():
                try:
                    ms = await page.evaluate(read_js)
                except Exception:
                    ms = state["prev_ms"]
                if ms and ms != state["prev_ms"]:
                    state["prev_ms"] = ms
                    state["last_activity"] = loop.time()

                now = loop.time()
                if self._should_check(
                    now,
                    state["last_activity"],
                    state["last_check"],
                    state["last_check_activity"],
                    self.idle_seconds,
                    self.max_interval,
                ):
                    state["last_check"] = now
                    state["last_check_activity"] = state["last_activity"]
                    with suppress(Exception):
                        await callback(self)

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass

        task = loop.create_task(watch())

        def cleanup() -> None:
            stop_event.set()
            task.cancel()
            with suppress(Exception):
                page.remove_listener("framenavigated", mark_activity)

        return cleanup

    async def check(self, page: "Page") -> DetectionResult:
        """Check condition using LLM vision."""
        try:
            # Import litellm only when needed
            try:
                from litellm import acompletion
            except ImportError:
                return DetectionResult(
                    matched=False,
                    detection_type=self.detection_type,
                    reason="LLM detection requires 'litellm' package. Install with: pip install browser-handoff[llm]",
                )

            # Take screenshot
            screenshot = await page.screenshot(type="jpeg", quality=80)
            base64_image = base64.b64encode(screenshot).decode("utf-8")

            # Call LLM
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}",
                                },
                            },
                            {
                                "type": "text",
                                "text": USER_PROMPT_TEMPLATE.format(
                                    condition=self.condition
                                ),
                            },
                        ],
                    },
                ],
                "max_tokens": 10,
            }
            if self.api_key:
                kwargs["api_key"] = self.api_key
            response = await acompletion(**kwargs)

            # Parse response
            answer = response.choices[0].message.content.strip().lower()
            matched = answer == "yes"

            return DetectionResult(
                matched=matched,
                detection_type=self.detection_type,
                reason=f"LLM responded '{answer}' to condition: {self.condition}",
                details={"model": self.model, "condition": self.condition, "answer": answer},
            )

        except Exception as e:
            return DetectionResult(
                matched=False,
                detection_type=self.detection_type,
                reason=f"LLM check failed: {e}",
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        result: dict[str, Any] = {
            "type": self.detection_type,
            "model": self.model,
            "condition": self.condition,
            "idle_seconds": self.idle_seconds,
            "max_interval": self.max_interval,
        }
        if self.api_key:
            result["api_key"] = self.api_key
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMDetection":
        """Create from dictionary."""
        return cls(
            model=data.get("model", "anthropic/claude-sonnet-4-5"),
            condition=data.get("condition", ""),
            api_key=data.get("api_key"),
            idle_seconds=data.get("idle_seconds", 2.0),
            max_interval=data.get("max_interval", 30.0),
        )
