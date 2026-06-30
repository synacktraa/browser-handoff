# /// script
# requires-python = ">=3.12"
# dependencies = ["daytona", "rich"]
# ///
"""
Run examples/claude_oauth_login_handoff/local.py inside a Daytona sandbox.

Same browser-handoff + ccauth OAuth flow as `local.py`, but Chrome,
the streaming server, and ccauth's callback listener all run inside a
Daytona sandbox. The human opens the Daytona preview URL to sign in.

What this script does per run:
  1. Build (or reuse) the sandbox image (debian-slim + uv + patchright + Chrome).
  2. Create a sandbox; resolve a signed preview URL for port 8080.
  3. `curl` local.py from master, run it under xvfb with
     `--public-base <preview-url>`.
  4. Tail sandbox stdout; local.py's Rich panels pass through with
     ANSI colors intact.

Environment Variables:
    DAYTONA_API_KEY:      required, your Daytona API key.
    DISCORD_WEBHOOK_URL:  optional, forwarded into the sandbox.

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
    """Declarative image with everything local.py needs to run unattended."""
    from daytona import Image

    return (
        Image.debian_slim("3.12")
        .run_commands(
            # curl: fetch local.py. xvfb+xauth: headless X for headed Chrome.
            # git: uv resolves git+https script deps.
            "apt-get update && "
            "apt-get install -y --no-install-recommends "
            "curl ca-certificates xvfb xauth dbus-x11 git && "
            "rm -rf /var/lib/apt/lists/*",
            # uv installer lands in /root/.local/bin; symlink to PATH.
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "ln -sf /root/.local/bin/uv /usr/local/bin/uv",
        )
        # Bake Chrome into the image so sandbox boot doesn't redownload ~200MB.
        .pip_install("patchright>=1.48")
        .run_commands("patchright install --with-deps chrome")
    )


def _extract_log_text(log: object) -> str:
    """Pull log content out of Daytona's response object.

    Stringifying the response gives the repr — newlines are escaped and
    the length drifts as inner fields grow, breaking seen-bytes diffing.
    Read the fields directly.
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
    """Stream the command's stdout/stderr to this terminal verbatim.

    No per-line prefix — local.py emits Rich panels with Unicode
    borders, and a prefix would break them. Raw passthrough also
    preserves the ANSI colors.
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

    # Forward DISCORD_WEBHOOK_URL if set. UV_NO_PROGRESS suppresses
    # uv's cursor-control codes — noise in a captured-log demo.
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

            # Signed preview URL: token is in the URL, so the human
            # opens it without a daytona.io login or a custom header.
            # 1h validity — well past access + completion timeouts,
            # with slack if the operator steps away.
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
