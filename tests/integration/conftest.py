"""Shared fixtures for integration tests.

Tests run headless. To debug a specific failure visually, drop
`await page.pause()` into the test — Playwright Inspector opens and
you can step through interactively.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, async_playwright

from .pages import ROUTES


# ---- Local HTTP server --------------------------------------------------


class _RouteHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        # Strip query string when matching routes.
        path = self.path.split("?", 1)[0]
        body = ROUTES.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # suppress per-request stderr noise


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    """Spin up a localhost HTTP server serving the crafted pages."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RouteHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


# ---- Playwright browser & page ------------------------------------------


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:
            # Don't dump 55 tracebacks at the user when the cause is one
            # missing browser binary — exit the whole run with a clear hint.
            if "Executable doesn't exist" in str(e):
                pytest.exit(
                    "\n"
                    "Integration tests require Chromium for Playwright,\n"
                    "but it isn't installed. Run:\n"
                    "\n"
                    "    uv run playwright install chromium --with-deps\n"
                    "\n"
                    "then re-run pytest.",
                    returncode=1,
                )
            raise
        try:
            yield browser
        finally:
            await browser.close()


@pytest_asyncio.fixture(loop_scope="session")
async def page(browser: Browser) -> AsyncIterator[Page]:
    """Fresh context + page per test for clean state isolation."""
    context = await browser.new_context()
    page = await context.new_page()
    try:
        yield page
    finally:
        await context.close()
