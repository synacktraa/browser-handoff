"""LLM-based detection using vision models."""

from __future__ import annotations

import asyncio
import base64
import hashlib
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

# Frame diff threshold (percentage of pixels that must differ)
FRAME_DIFF_THRESHOLD = 0.05  # 5% change triggers check


def _compute_frame_hash(image_bytes: bytes) -> str:
    """Compute a simple hash of image bytes for comparison."""
    return hashlib.md5(image_bytes).hexdigest()


@dataclass
class LLMDetection(BaseDetection):
    """Detection based on LLM analysis of screenshots.

    Uses LiteLLM for model inference. Triggers on significant visual changes.

    Example:
        detection = LLMDetection(
            model="anthropic/claude-sonnet-4-5",
            condition="The page is showing a CAPTCHA or security challenge",
        )
    """

    detection_type: str = field(default="llm", init=False)

    model: str = "anthropic/claude-sonnet-4-5"
    condition: str = ""

    # Internal state for frame diff detection
    _last_frame_hash: str | None = field(default=None, init=False, repr=False)
    _check_interval: float = field(default=2.0, init=False, repr=False)  # seconds

    def register_listeners(
        self,
        page: "Page",
        callback: Callable[["BaseDetection"], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register periodic screenshot comparison."""
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        async def periodic_check() -> None:
            while not stop_event.is_set():
                try:
                    screenshot = await page.screenshot(type="jpeg", quality=50)
                    current_hash = _compute_frame_hash(screenshot)
                    if self._last_frame_hash is None or current_hash != self._last_frame_hash:
                        self._last_frame_hash = current_hash
                        await callback(self)
                except Exception:
                    pass

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._check_interval)
                except asyncio.TimeoutError:
                    pass

        task = loop.create_task(periodic_check())

        def cleanup() -> None:
            stop_event.set()
            task.cancel()

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
            response = await acompletion(
                model=self.model,
                messages=[
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
                                "text": USER_PROMPT_TEMPLATE.format(condition=self.condition),
                            },
                        ],
                    },
                ],
                max_tokens=10,
            )

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
        return {
            "type": self.detection_type,
            "model": self.model,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMDetection":
        """Create from dictionary."""
        return cls(
            model=data.get("model", "anthropic/claude-sonnet-4-5"),
            condition=data.get("condition", ""),
        )
