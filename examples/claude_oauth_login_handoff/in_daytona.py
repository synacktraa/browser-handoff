# /// script
# requires-python = ">=3.12"
# dependencies = ["daytona"]
# ///
"""
Run examples/claude_oauth_login_handoff/local.py inside a Daytona sandbox.

Story: the same browser-handoff + ccauth OAuth flow as `local.py`, but
the whole thing — Chrome, the streaming server, ccauth's callback
listener — runs inside a Daytona sandbox. The human just opens the
Daytona preview URL to log in.

Architecture (compared to a local run):
  - local.py drives Chrome (patchright) inside the sandbox under xvfb
  - browser-handoff's streaming server binds 0.0.0.0:8080 in the sandbox
  - Daytona exposes port 8080 via a preview URL
  - this script passes that preview URL into local.py as --public-base
    so the human-facing stream link in logs/notifications uses it
  - CDP traffic stays loopback inside the sandbox; only the JPEG frames
    and control messages cross the wire (same wire profile as any
    cloud-hosted browser session)

Image build (declarative, runs once and is cached by Daytona):
  - debian-slim with python 3.12
  - apt: curl, ca-certs, xvfb, xauth, dbus-x11
  - uv (via the official installer, symlinked into /usr/local/bin)
  - patchright + Chrome (baked into the image so sandbox boot is fast)

Boot sequence per run:
  1. Create sandbox from the image (cached after first build)
  2. Resolve the Daytona preview URL for port 8080
  3. `curl` the latest local.py from this repo on master
  4. `xvfb-run -a uv run local.py --public-base <preview-url>`
  5. Tail sandbox logs to this terminal so the operator sees the
     stream URL that browser-handoff prints

Environment Variables:
    DAYTONA_API_KEY:      required, your Daytona API key
    DISCORD_WEBHOOK_URL:  optional, forwarded into the sandbox so
                          browser-handoff can push login notifications

Run:
    uv run examples/claude_oauth_login_handoff/in_daytona.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daytona import AsyncSandbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

LOCAL_PY_URL = (
    "https://raw.githubusercontent.com/synacktraa/browser-handoff/master/"
    "examples/claude_oauth_login_handoff/local.py"
)

STREAMING_PORT = 8080
SESSION_ID = "browser-handoff-oauth"


def _build_image():
    """Declarative image: everything local.py needs to run unattended."""
    from daytona import Image

    return (
        Image.debian_slim("3.12")
        .run_commands(
            # System packages: curl for fetching local.py, xvfb+xauth for
            # a headless X display so patchright's headed Chrome can run.
            "apt-get update && "
            "apt-get install -y --no-install-recommends "
            "curl ca-certificates xvfb xauth dbus-x11 git && "
            "rm -rf /var/lib/apt/lists/*",
            # uv: official installer drops the binary into /root/.local/bin;
            # symlink into /usr/local/bin so `uv` is on PATH for any shell.
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "ln -sf /root/.local/bin/uv /usr/local/bin/uv",
        )
        # Bake patchright + Chrome into the image so each sandbox boot
        # doesn't re-download ~200MB of browser binary.
        .pip_install("patchright>=1.48")
        .run_commands(
            "patchright install chrome",
            "patchright install-deps chrome",
        )
    )


def _extract_log_text(log: object) -> str:
    """Pull the actual log content out of Daytona's response object.

    Stringifying the response itself gives the repr, which escapes real
    newlines as backslash-n inside `stderr='...'` and changes length
    per call as inner fields grow — that breaks both rendering and
    seen-bytes diffing. Read the actual fields instead.
    """
    if log is None:
        return ""
    if isinstance(log, str):
        return log
    # Some SDK versions surface a combined `output`; others split into
    # `stdout` / `stderr`. Prefer whichever is populated.
    output = getattr(log, "output", None) or ""
    if output:
        return output
    stdout = getattr(log, "stdout", None) or ""
    stderr = getattr(log, "stderr", None) or ""
    return stdout + stderr


async def _tail_logs(sandbox: AsyncSandbox, cmd_id: str) -> None:
    """Stream the running command's stdout/stderr to this terminal.

    Daytona's get_session_command_logs returns the full log buffer each
    call — we track how much we've already printed and only emit new
    bytes. Stops when the command finishes.
    """
    seen = 0
    while True:
        try:
            log = await sandbox.process.get_session_command_logs(SESSION_ID, cmd_id)
            text = _extract_log_text(log)
        except Exception as e:
            logger.warning("log fetch failed: %s", e)
            text = ""

        if len(text) > seen:
            for line in text[seen:].splitlines():
                if line:
                    logger.info("[sandbox] %s", line)
            seen = len(text)

        try:
            info = await sandbox.process.get_session_command(SESSION_ID, cmd_id)
            if getattr(info, "exit_code", None) is not None:
                return
        except Exception:
            pass

        await asyncio.sleep(1.0)


async def main() -> None:
    from daytona import (
        AsyncDaytona,
        CreateSandboxFromImageParams,
        Resources,
        SessionExecuteRequest,
    )

    # Forward DISCORD_WEBHOOK_URL into the sandbox if the operator set it
    # locally; everything else local.py needs is fetched on the fly.
    env_vars: dict[str, str] = {}
    if webhook := os.getenv("DISCORD_WEBHOOK_URL"):
        env_vars["DISCORD_WEBHOOK_URL"] = webhook

    async with AsyncDaytona() as daytona:
        logger.info("Creating sandbox (first run builds the image ~1-2GB; cached after)...")
        sandbox = await daytona.create(
            CreateSandboxFromImageParams(
                image=_build_image(),
                resources=Resources(cpu=2, memory=4, disk=10),
                env_vars=env_vars or None,
            ),
            timeout=600,
            on_snapshot_create_logs=lambda log: logger.info("[image] %s", log),
        )
        logger.info("Sandbox %s ready", sandbox.id)

        try:
            await sandbox.process.create_session(SESSION_ID)

            # Signed preview URL: the token is embedded in the URL, so the
            # human can open it without being logged into daytona.io and
            # without setting an x-daytona-preview-token header. Valid for
            # 1 hour — well beyond the 10-minute completion_timeout, but
            # enough buffer if the operator steps away briefly.
            preview = await sandbox.create_signed_preview_url(
                STREAMING_PORT, expires_in_seconds=3600
            )
            preview_url = preview.url
            logger.info("=" * 70)
            logger.info("Sandbox streaming preview: %s", preview_url)
            logger.info("(the human-facing URL with ?session=... will appear")
            logger.info(" in the sandbox logs below once local.py starts)")
            logger.info("=" * 70)

            # Quote the URL — Daytona preview hosts contain dots and dashes
            # but no shell metacharacters; shlex.quote is the safe default.
            command = (
                f"curl -fsSL {shlex.quote(LOCAL_PY_URL)} -o /tmp/local.py && "
                f"xvfb-run -a uv run /tmp/local.py "
                f"--public-base {shlex.quote(preview_url)}"
            )
            cmd = await sandbox.process.execute_session_command(
                SESSION_ID,
                SessionExecuteRequest(command=command, run_async=True),
            )
            cmd_id = getattr(cmd, "cmd_id", None) or getattr(cmd, "id", None)
            if not cmd_id:
                raise RuntimeError("Daytona did not return a command id")
            logger.info("Started local.py in sandbox (cmd_id=%s)", cmd_id)

            await _tail_logs(sandbox, cmd_id)
        finally:
            logger.info("Deleting sandbox")
            await sandbox.delete()


if __name__ == "__main__":
    asyncio.run(main())
