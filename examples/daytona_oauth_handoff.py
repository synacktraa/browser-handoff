# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "daytona",
#   "patchright",
#   "browser-handoff",
#   "httpx",
# ]
# ///
"""
Example: Claude OAuth with browser-handoff in Daytona Sandbox.

This example demonstrates using browser-handoff for human-in-the-loop
OAuth authentication running inside a Daytona sandbox.

Features:
- Full OAuth 2.0 PKCE flow for Claude Code authentication
- Local callback server to capture authorization code
- browser-handoff streaming for human login intervention
- Persistent volume for browser profile (maintains login sessions)
- CDP-based remote browser connection

Requirements:
    pip install daytona playwright browser-handoff httpx

Environment Variables:
    DAYTONA_API_KEY: Your Daytona API key
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import textwrap
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any, AsyncIterator
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

if TYPE_CHECKING:
    from daytona import AsyncSandbox
    from playwright.async_api import Browser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================================
# Claude OAuth Constants (from ccauth)
# =============================================================================

AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://api.anthropic.com/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
SCOPE = (
    "org:create_api_key user:profile user:inference "
    "user:sessions user:mcp_servers user:file_upload"
)
CALLBACK_PATH = "/callback"

# Anthropic's edge returns fake 429 for python-requests/*; axios UA bypasses it
USER_AGENT = "axios/1.13.6"


# =============================================================================
# OAuth Primitives: PKCE, state, token exchange
# =============================================================================


@dataclass
class PKCE:
    verifier: str
    challenge: str


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    expires_at_ms: int
    scopes: list[str]
    raw: dict[str, Any]


def generate_pkce() -> PKCE:
    """Generate PKCE code verifier and challenge."""
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return PKCE(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    """Generate random state parameter."""
    return secrets.token_hex(32)


def build_authorize_url(
    *,
    redirect_uri: str,
    code_challenge: str,
    state: str,
) -> str:
    """Build the OAuth authorization URL."""
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    state: str,
    timeout: float = 15.0,
) -> TokenResult:
    """Exchange authorization code for tokens."""
    body = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    response = httpx.post(
        TOKEN_URL,
        headers={"User-Agent": USER_AGENT},
        json=body,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed: {response.status_code} {response.text}"
        )

    data = response.json()
    missing = [k for k in ("access_token", "refresh_token", "expires_in") if k not in data]
    if missing:
        raise RuntimeError(f"Token response missing fields: {missing}")

    scopes = data["scope"].split(" ") if data.get("scope") else []
    return TokenResult(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at_ms=int(time.time() * 1000) + data["expires_in"] * 1000,
        scopes=scopes,
        raw=data,
    )


# =============================================================================
# Local Callback Server
# =============================================================================

_SUCCESS_HTML = """\
<!DOCTYPE html>
<html><head><title>Success</title>
<style>body{font-family:system-ui,sans-serif;background:#f5f5f5;display:flex;
justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#fff;padding:40px;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,.1);
text-align:center;max-width:400px}h1{color:#4CAF50;margin-bottom:10px}p{color:#666}
</style></head><body><div class="card"><h1>Login Successful</h1>
<p>You may close this window and return to the terminal.</p></div></body></html>\
"""

_ERROR_HTML = """\
<!DOCTYPE html>
<html><head><title>Error</title>
<style>body{font-family:system-ui,sans-serif;background:#f5f5f5;display:flex;
justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#fff;padding:40px;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,.1);
text-align:center;max-width:400px}h1{color:#f44336;margin-bottom:10px}p{color:#666}
</style></head><body><div class="card"><h1>Login Failed</h1><p>%s</p></div></body></html>\
"""


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server: _CallbackHTTPServer = self.server  # type: ignore[assignment]
        parsed = urlparse(self.path)

        if parsed.path != server.callback_path:
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]

        if not code:
            error = (params.get("error") or ["Unknown error"])[0]
            self._send_html(400, _ERROR_HTML % error)
            server.error = error
            return

        if state != server.expected_state:
            self._send_html(400, _ERROR_HTML % "Invalid state parameter")
            server.error = "state mismatch"
            return

        server.auth_code = code
        self._send_html(200, _SUCCESS_HTML)

    def _send_html(self, status: int, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args, **_kwargs) -> None:
        pass  # Silence access logs


class _CallbackHTTPServer(HTTPServer):
    expected_state: str
    callback_path: str
    auth_code: str | None
    error: str | None


@dataclass
class CallbackServer:
    """Local HTTP server to capture OAuth callback."""

    port: int
    callback_path: str
    _server: _CallbackHTTPServer
    _thread: threading.Thread

    def wait_for_code(self, timeout: float = 300.0) -> str:
        """Wait for the authorization code from callback."""
        self._thread.join(timeout=timeout)
        try:
            if self._server.auth_code:
                return self._server.auth_code
            if self._server.error:
                raise RuntimeError(f"OAuth callback error: {self._server.error}")
            raise RuntimeError("Timed out waiting for OAuth callback")
        finally:
            self._server.server_close()


def start_callback_server(expected_state: str) -> CallbackServer:
    """Start a local callback server on a random available port."""
    server = _CallbackHTTPServer(("localhost", 0), _CallbackHandler)
    server.expected_state = expected_state
    server.callback_path = CALLBACK_PATH
    server.auth_code = None
    server.error = None

    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    return CallbackServer(
        port=actual_port,
        callback_path=CALLBACK_PATH,
        _server=server,
        _thread=thread,
    )


# =============================================================================
# Daytona Sandbox with Browser Support
# =============================================================================

# Browser launcher script that runs inside the sandbox
_BROWSER_LAUNCHER = textwrap.dedent('''
    import asyncio
    from patchright.async_api import async_playwright

    async def main():
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir="{profile_path}",
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--remote-debugging-port=9222",
                    "--remote-debugging-address=0.0.0.0",
                ],
            )
            # Keep browser running until process is killed
            while True:
                await asyncio.sleep(1)

    asyncio.run(main())
''')

# Volume for persistent browser profile
PROFILE_VOLUME_ID = "browser-handoff-profile"
PROFILE_MOUNT_PATH = "/home/daytona/.browser-profile"
CDP_PORT = 9222
STREAMING_PORT = 8080


def _build_browser_image():
    """Build Daytona image with browser dependencies."""
    from daytona import Image

    return (
        Image.debian_slim("3.12")
        .run_commands(
            "apt-get update && "
            "apt-get install -y --no-install-recommends xvfb xauth && "
            "rm -rf /var/lib/apt/lists/*",
        )
        .pip_install("patchright")
        .run_commands("patchright install chromium")
    )


class BrowserEnabledSandbox:
    """Sandbox wrapper with browser support.

    Provides access to both the Daytona sandbox and a Playwright
    browser instance running inside it via CDP.

    Attributes:
        sandbox: The underlying AsyncSandbox instance (typed for IDE support)
        browser: Playwright Browser connected via CDP
    """

    def __init__(self, sandbox: "AsyncSandbox", browser: "Browser"):
        self._sandbox = sandbox
        self._browser = browser

    @property
    def sandbox(self) -> "AsyncSandbox":
        """The underlying Daytona sandbox (typed for IDE completion)."""
        return self._sandbox

    @property
    def browser(self) -> "Browser":
        """Playwright Browser connected via CDP."""
        return self._browser


async def _get_cdp_ws_url(base_url: str) -> str:
    """Get WebSocket URL from Chrome's /json/version endpoint."""
    import aiohttp

    version_url = f"{base_url}/json/version"

    async with aiohttp.ClientSession() as session:
        async with session.get(version_url) as resp:
            data = await resp.json()
            return data["webSocketDebuggerUrl"]


@asynccontextmanager
async def create_browser_enabled_sandbox() -> AsyncIterator[BrowserEnabledSandbox]:
    """Create a Daytona sandbox with browser support for human-in-the-loop tasks.

    This context manager:
    1. Creates a sandbox with Patchright/Chromium pre-installed
    2. Mounts a persistent volume for browser profile data
    3. Launches browser inside sandbox with CDP exposed
    4. Connects to browser via CDP from local machine
    5. Yields a BrowserEnabledSandbox with .browser (Playwright Browser) and .sandbox
    6. Cleans up browser and sandbox on exit

    The persistent volume maintains browser state (cookies, localStorage, etc.)
    across sandbox runs, reducing repeated login prompts.

    Yields:
        BrowserEnabledSandbox with .browser (Playwright Browser) and .sandbox properties.

    Example:
        async with create_browser_enabled_sandbox() as ctx:
            # Access browser contexts and pages
            context = ctx.browser.contexts[0]
            page = context.pages[0]
            await page.goto("https://example.com")

            # Use browser-handoff for human intervention
            result = await handoff.wait_if_blocked(page, context)

            # Access sandbox methods with full type support
            response = await ctx.sandbox.process.exec("echo 'Hello'")
    """
    from daytona import (
        AsyncDaytona,
        CreateSandboxFromImageParams,
        Resources,
        VolumeMount,
    )
    from playwright.async_api import async_playwright

    async with AsyncDaytona() as daytona:
        # Get or create persistent volume for browser profile
        volume = await daytona.volume.get(PROFILE_VOLUME_ID, create=True)
        logger.info("Using volume: %s", volume.id)

        # Create sandbox with browser image and volume
        sandbox = await daytona.create(
            CreateSandboxFromImageParams(
                image=_build_browser_image(),
                resources=Resources(cpu=2, memory=4, disk=10),
                volumes=[
                    VolumeMount(
                        volume_id=volume.id,
                        mount_path=PROFILE_MOUNT_PATH,
                    )
                ],
            ),
            timeout=300,  # Image build may take time on first run
        )
        logger.info("Sandbox created: %s", sandbox.id)

        try:
            # Upload and run browser launcher script inside sandbox
            launcher_code = _BROWSER_LAUNCHER.format(profile_path=PROFILE_MOUNT_PATH)
            launcher_path = "/tmp/browser_launcher.py"
            await sandbox.fs.upload_file(launcher_code.encode(), launcher_path)

            # Start browser in background with xvfb
            await sandbox.process.exec(
                f"nohup xvfb-run -a python {launcher_path} > /tmp/browser.log 2>&1 &",
            )
            logger.info("Browser launcher started in sandbox")

            # Wait for browser to be ready
            await asyncio.sleep(3)

            # Get preview URL for CDP port
            preview = await sandbox.get_preview_link(CDP_PORT)
            cdp_base_url = f"https://{preview.url}"
            logger.info("CDP preview URL: %s", cdp_base_url)

            # Get WebSocket URL and connect via CDP
            ws_url = await _get_cdp_ws_url(cdp_base_url)
            # Replace the host in ws_url with preview URL
            ws_url = ws_url.replace("ws://localhost:9222", f"wss://{preview.url}")
            ws_url = ws_url.replace("ws://127.0.0.1:9222", f"wss://{preview.url}")
            logger.info("Connecting to CDP: %s", ws_url)

            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(ws_url)
                logger.info("Connected to browser via CDP")

                yield BrowserEnabledSandbox(sandbox=sandbox, browser=browser)

        finally:
            await sandbox.delete()
            logger.info("Sandbox deleted")


# =============================================================================
# Claude OAuth Flow with browser-handoff
# =============================================================================


async def run_claude_oauth() -> dict[str, Any]:
    """Run the complete Claude OAuth flow with human-in-the-loop handoff.

    This function:
    1. Starts a local callback server to capture the OAuth code
    2. Creates a Daytona sandbox with browser support
    3. Configures browser-handoff for human login intervention
    4. Navigates to the authorization URL
    5. Waits for human to complete login if needed
    6. Clicks the Authorize button
    7. Exchanges the code for tokens

    Returns:
        Dict containing access_token, refresh_token, expires_at_ms, and scopes.
    """
    from browser_handoff import Detection, Handoff, Scenario, ServerConfig

    # Generate PKCE and state for security
    pkce = generate_pkce()
    state = generate_state()

    # Start local callback server
    callback_server = start_callback_server(expected_state=state)
    redirect_uri = f"http://localhost:{callback_server.port}{CALLBACK_PATH}"
    logger.info("Callback server listening on %s", redirect_uri)

    # Build authorization URL
    authorize_url = build_authorize_url(
        redirect_uri=redirect_uri,
        code_challenge=pkce.challenge,
        state=state,
    )
    logger.info("Authorization URL: %s", authorize_url)

    async with create_browser_enabled_sandbox() as ctx:
        context = ctx.browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # Get signed preview URL for streaming server public base
        streaming_preview = await ctx.sandbox.create_signed_preview_url(
            STREAMING_PORT, expires_in_seconds=3600
        )
        public_base = f"https://{streaming_preview.url}"
        logger.info("Streaming public base: %s", public_base)

        # Configure handoff for Claude OAuth flow
        handoff = Handoff(
            scenarios=[
                Scenario(
                    name="Claude Login",
                    trigger=Detection.url(path_contains=["/login"]),
                    complete=Detection.url(path_contains=["/oauth/authorize"]),
                ),
            ],
            server=ServerConfig(
                port=STREAMING_PORT,
                public_base=public_base,
            ),
        )

        # Navigate to authorization URL
        logger.info("Navigating to authorization URL...")
        await page.goto(authorize_url, wait_until="domcontentloaded")

        # Check if human intervention needed (login required)
        result = await handoff.wait_if_blocked(page, context, trigger_timeout=10)

        if result.was_blocked:
            logger.info("Human completed login: %s", result.scenario_name)

        # Click authorize button (bot action after human login)
        # Wait up to 60s for Cloudflare Turnstile to clear
        logger.info("Waiting for Authorize button...")
        authorize_btn = page.get_by_role("button", name="Authorize", exact=True).first
        await authorize_btn.wait_for(state="visible", timeout=60000)
        await authorize_btn.click()
        logger.info("Clicked Authorize button")

        # Wait for redirect to callback
        await page.wait_for_url(f"**/localhost:{callback_server.port}/**", timeout=15000)

    # Get authorization code from callback server
    code = callback_server.wait_for_code(timeout=30.0)
    logger.info("Received authorization code")

    # Exchange code for tokens
    tokens = exchange_code(
        code=code,
        code_verifier=pkce.verifier,
        redirect_uri=redirect_uri,
        state=state,
    )
    logger.info("Token exchange successful")

    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at_ms": tokens.expires_at_ms,
        "scopes": tokens.scopes,
    }


async def main():
    """Run the Claude OAuth flow and print the result."""
    try:
        result = await run_claude_oauth()
        logger.info("OAuth successful!")
        logger.info("Access token: %s...", result["access_token"][:20])
        logger.info("Refresh token: %s...", result["refresh_token"][:20])
        logger.info("Expires at: %s", result["expires_at_ms"])
        logger.info("Scopes: %s", result["scopes"])
    except Exception as e:
        logger.error("OAuth failed: %s", e)
        raise


if __name__ == "__main__":
    asyncio.run(main())
