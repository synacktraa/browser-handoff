# /// script
# requires-python = ">=3.12"
# dependencies = ["daytona", "rich"]
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
  - Daytona exposes port 8080 via a signed preview URL (token in the URL,
    so the operator can open it without being logged into daytona.io)
  - this script passes that preview URL into local.py as --public-base
    so the human-facing stream link in the rich panel and notifier
    messages uses it
  - CDP traffic stays loopback inside the sandbox; only the JPEG frames
    and control messages cross the wire (same wire profile as any
    cloud-hosted browser session)

Image build (declarative, runs once and is cached by Daytona):
  - debian-slim with python 3.12
  - apt: curl, ca-certs, xvfb, xauth, dbus-x11, git
  - uv (via the official installer, symlinked into /usr/local/bin)
  - patchright + Chrome (baked into the image so sandbox boot is fast)

Boot sequence per run:
  1. Create sandbox from the image (cached after first build)
  2. Resolve a signed Daytona preview URL for port 8080 (1h validity)
  3. `curl` the latest local.py from this repo on master
  4. `xvfb-run -a uv run local.py --public-base <preview-url>`
  5. Tail sandbox stdout to this terminal — local.py's rich panels
     come through with ANSI colors intact

Environment Variables:
    DAYTONA_API_KEY:      required, your Daytona API key
    DISCORD_WEBHOOK_URL:  optional, forwarded into the sandbox so
                          browser-handoff can push login notifications

Run:
    uv run examples/claude_oauth_login_handoff/in_daytona.py
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from daytona import AsyncSandbox

console = Console()

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
            # a headless X display so patchright's headed Chrome can run,
            # git so uv can resolve our `git+https://...` script deps.
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
        .run_commands("patchright install --with-deps chrome")
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
    output = getattr(log, "output", None) or ""
    if output:
        return output
    stdout = getattr(log, "stdout", None) or ""
    stderr = getattr(log, "stderr", None) or ""
    return stdout + stderr


async def _tail_logs(sandbox: AsyncSandbox, cmd_id: str) -> None:
    """Stream the running command's stdout/stderr to this terminal verbatim.

    No per-line prefix — local.py emits multi-line rich panels with Unicode
    box-drawing; a `[sandbox]` prefix per line would break the borders.
    Raw passthrough also preserves the ANSI escape codes rich emitted, so
    panels render with colors in the operator's terminal.
    """
    seen = 0
    while True:
        try:
            log = await sandbox.process.get_session_command_logs(SESSION_ID, cmd_id)
            text = _extract_log_text(log)
        except Exception as e:
            console.print(f"[yellow]log fetch failed: {e}[/yellow]")
            text = ""

        if len(text) > seen:
            sys.stdout.write(text[seen:])
            sys.stdout.flush()
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

    # Forward DISCORD_WEBHOOK_URL into the sandbox if set, and silence
    # uv's progress bar (the `\x02\x02\x02` cursor-control codes) since it
    # adds nothing to a captured-log demo.
    env_vars: dict[str, str] = {"UV_NO_PROGRESS": "1"}
    if webhook := os.getenv("DISCORD_WEBHOOK_URL"):
        env_vars["DISCORD_WEBHOOK_URL"] = webhook

    async with AsyncDaytona() as daytona:
        console.rule("[bold]browser-handoff × Daytona — Claude OAuth[/bold]")
        console.print(
            "[cyan]→[/cyan] Creating sandbox "
            "(first run builds the image; cached after)"
        )
        sandbox = await daytona.create(
            CreateSandboxFromImageParams(
                image=_build_image(),
                resources=Resources(cpu=2, memory=4, disk=10),
                env_vars=env_vars,
            ),
            timeout=600,
            on_snapshot_create_logs=lambda log: console.print(
                f"  [dim]{log.rstrip()}[/dim]"
            ),
        )
        console.print(f"[cyan]→[/cyan] Sandbox [bold]{sandbox.id}[/bold] ready")

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
            console.print(
                f"[cyan]→[/cyan] Signed preview URL: "
                f"[link={preview_url}]{preview_url}[/link]"
            )
            console.print(
                "  [dim](fallback if the Discord notification doesn't reach you)[/dim]"
            )
            console.print("[cyan]→[/cyan] Booting local.py inside sandbox...")
            console.print()

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

            await _tail_logs(sandbox, cmd_id)
        finally:
            console.print()
            console.print("[cyan]→[/cyan] Deleting sandbox")
            await sandbox.delete()


if __name__ == "__main__":
    asyncio.run(main())
