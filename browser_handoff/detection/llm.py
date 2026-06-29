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
SYSTEM_PROMPT = """You decide when a human has finished an intervention \
task in a streamed browser session so an automation agent can resume.

Match against the task — not the literal condition. The condition is the \
agent's guess at the resume page and is often over-specific; the user may \
finish on an intermediate or downstream page (e.g. a post-action \
confirmation screen, a success banner on a different route, a redirect \
back to home with a logged-in indicator). Answer yes whenever the work \
the human was asked to do is observably done, regardless of which page \
they end up on.

Answer "no" while work is still in progress:
  * a form is being filled (focus in an input, partial values, blank \
required fields, visible validation errors).
  * the page is loading, transitioning, or showing a spinner mid-action.
  * a modal/overlay is obviously waiting for more human action.

Use the URL and title alongside the screenshot to disambiguate \
look-alike states.

Respond with only "yes" or "no"."""

USER_PROMPT_TEMPLATE = """Page URL: {url}
Page title: {title}
{reason_block}
Agent's expected end state (its guess, may be over-specific): {condition}

Has the human's underlying intervention task completed? Answer "yes" if \
the task implied by the reason / condition is observably done on this page \
— even on an intermediate or downstream page from the one the condition \
literally describes. Otherwise "no". Respond with only "yes" or "no"."""

# Rendered into USER_PROMPT_TEMPLATE only when a `reason` is passed via
# **context; omitted otherwise so the prompt doesn't carry an empty heading.
_REASON_BLOCK_TEMPLATE = "\nTask given to the human: {reason}\n"


def _activity_setup_js(var: str) -> str:
    """Inject capture/passive listeners that stamp `window[var]` on input.

    `var` is non-enumerable and per-session random — site JS can't list
    or probe it. Listeners only fire on deliberate input (mousedown,
    keydown, wheel, scroll, touchstart, input, paste). Mousemove and
    DOM mutations are deliberately excluded: hover and page-driven
    changes (carousels, ads, analytics) aren't operator activity.
    """
    return (
        "(() => {"
        f"if (window.{var} !== undefined) return;"
        f"Object.defineProperty(window, '{var}', "
        "{value: 0, writable: true, enumerable: false, configurable: true});"
        f"const mark = () => {{ window.{var} = Date.now(); }};"
        "const opts = {capture: true, passive: true};"
        "for (const e of ['mousedown','keydown','wheel','scroll','touchstart','input','paste']) {"
        "  window.addEventListener(e, mark, opts);"
        "}"
        "})();"
    )


def _activity_read_js(var: str) -> str:
    """JS that returns the latest activity stamp (0 if none yet)."""
    return f"() => window.{var} || 0"


@dataclass
class LLMDetection(BaseDetection):
    """Vision-LLM detection: screenshot + prompt, answer yes/no.

    Checks fire on idle-settle (operator stops interacting for
    `idle_seconds`), with a `max_interval` safety net for async page
    work the input listeners can't see (canvas, video, late XHR).

    Example:
        LLMDetection(
            model="anthropic/claude-sonnet-4-5",
            condition="The page is showing a login form",
            api_key="sk-ant-...",   # optional; falls back to provider env var
            idle_seconds=3.0,
            max_interval=30.0,
        )
    """

    detection_type: str = field(default="llm", init=False)

    model: str = "anthropic/claude-sonnet-4-5"
    condition: str = ""
    # If None, litellm reads the key from the provider's env var
    # (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, ...).
    api_key: str | None = None

    # Debounce window: fire once activity has been quiet this long.
    # The dominant cost knob — larger means fewer, later checks.
    idle_seconds: float = 3.0
    # Safety net: fire at least this often after the first activity.
    # 0 disables it (debounce-only).
    max_interval: float = 30.0

    # JS stamp poll cadence. Cheap (reads a number); not a public knob.
    _poll_interval: float = field(default=0.25, init=False, repr=False)

    def __post_init__(self) -> None:
        """Verify litellm is importable; surface a missing [llm] extra now,
        not minutes into a flow when the first vision check fires."""
        try:
            import litellm  # noqa: F401  (import for side-effect verification)
        except ImportError as e:
            raise ImportError(
                "LLMDetection requires the 'litellm' package. "
                "Install with: pip install browser-handoff[llm]"
            ) from e

    @staticmethod
    def _should_check(
        now: float,
        last_activity: float | None,
        last_check: float,
        last_check_activity: float | None,
        idle_seconds: float,
        max_interval: float,
    ) -> bool:
        """Decide whether to fire a check this tick (pure timing function).

        Fires when EITHER:
          - settled: activity is newer than the last check covered AND the
            page has been quiet for `idle_seconds`; OR
          - stale: `max_interval` has elapsed since the last check.

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
        """Install the in-page activity watcher and run the check loop.

        Mode-agnostic: operator input becomes real DOM events on the
        page in both streaming (via the WS handler's CDP dispatch) and
        passthrough (via the substrate's viewer), so the same listeners
        catch them. The loop polls the stamp and fires the callback when
        `_should_check` says so.

        Args:
            page: Playwright page to observe.
            callback: Async function invoked with `self` when a check
                is due.

        Returns:
            A cleanup function that stops the loop and removes listeners.
        """
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        # Per-session, non-enumerable property name (see _activity_setup_js).
        activity_var = f"__bh_{secrets.token_hex(8)}"
        setup_js = _activity_setup_js(activity_var)
        read_js = _activity_read_js(activity_var)
        state: dict[str, Any] = {
            "last_activity": None,
            "last_check": loop.time(),
            "last_check_activity": None,
            "prev_ms": 0,
        }

        def mark_activity(*_args: Any) -> None:
            # Navigation counts as activity — old listeners can't fire
            # on a new document, so the stamp won't catch the change.
            state["last_activity"] = loop.time()

        page.on("framenavigated", mark_activity)

        # Re-inject on navigation so the new document gets a fresh
        # listener set on the same name (the setup script is idempotent).
        def _reinject(*_args: Any) -> None:
            if stop_event.is_set():
                return
            loop.create_task(_reinject_now())

        async def _reinject_now() -> None:
            with suppress(Exception):
                await page.evaluate(setup_js)

        page.on("framenavigated", _reinject)

        async def watch() -> None:
            # add_init_script covers future documents; evaluate covers
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
            with suppress(Exception):
                page.remove_listener("framenavigated", _reinject)

        return cleanup

    async def check(self, page: "Page", **context: Any) -> DetectionResult:
        """Ask the model whether the condition holds on the current page.

        Args:
            page: Playwright page to screenshot.
            **context: Reads `reason` — the operator-facing explanation
                shown in the wrapper — to ground the prompt. More
                informative than `condition` alone, which is the caller's
                guess at the resume state.
        """
        from litellm import acompletion

        try:
            screenshot = await page.screenshot(type="jpeg", quality=80)
            base64_image = base64.b64encode(screenshot).decode("utf-8")

            # URL + title disambiguate look-alike screenshots (partial
            # form fill vs. successful submission landing page). All
            # captured defensively — a missing string is fine, but a
            # raised exception would abort the whole check.
            url = ""
            title = ""
            try:
                url = page.url or ""
            except Exception:
                pass
            try:
                title = await page.title()
            except Exception:
                pass
            reason_block = ""
            ctx_reason = context.get("reason")
            if ctx_reason:
                reason_block = _REASON_BLOCK_TEMPLATE.format(reason=ctx_reason)

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
                                    url=url or "(unavailable)",
                                    title=title or "(unavailable)",
                                    reason_block=reason_block,
                                    condition=self.condition,
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
        return cls(
            model=data.get("model", "anthropic/claude-sonnet-4-5"),
            condition=data.get("condition", ""),
            api_key=data.get("api_key"),
            idle_seconds=data.get("idle_seconds", 3.0),
            max_interval=data.get("max_interval", 30.0),
        )
