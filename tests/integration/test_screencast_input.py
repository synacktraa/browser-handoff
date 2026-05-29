"""Integration tests for forwarded input on a streamed page.

These exercise StreamingServer's input handlers (_handle_keyboard /
_handle_mouse) and the context-menu guard directly against a real Chromium
page over a CDP session — no running web server needed. They're the regression
net for "key/click X doesn't work in the stream" reports, most notably Enter:
CDP only performs a key's default action (submit / newline / character insert)
when the keyDown carries `text`, so Enter must send a carriage return.
"""

from __future__ import annotations

from playwright.async_api import CDPSession, Page

from browser_handoff.server.streaming import StreamingServer


async def _cdp(page: Page) -> CDPSession:
    return await page.context.new_cdp_session(page)


async def _key(server: StreamingServer, cdp: CDPSession, key: str, code: str) -> None:
    """Send a full keydown+keyup for one key."""
    await server._handle_keyboard(cdp, {"action": "keydown", "key": key, "code": code})
    await server._handle_keyboard(cdp, {"action": "keyup", "key": key, "code": code})


async def test_typing_inserts_characters(page: Page) -> None:
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content('<input id="i" autofocus>')
    await page.focus("#i")

    for ch, code in [("h", "KeyH"), ("i", "KeyI")]:
        await _key(server, cdp, ch, code)

    assert await page.input_value("#i") == "hi"


async def test_enter_submits_form(page: Page) -> None:
    """The Enter regression: without text='\\r' on keyDown, this never submits."""
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content(
        """
        <form id="f"><input id="i"></form>
        <script>
          window.__submitted = false;
          document.getElementById('f').addEventListener('submit', (e) => {
            e.preventDefault();           // don't actually navigate
            window.__submitted = true;
          });
        </script>
        """
    )
    await page.focus("#i")

    await _key(server, cdp, "Enter", "Enter")

    # Raises (failing the test) if Enter didn't trigger implicit submission.
    await page.wait_for_function("() => window.__submitted === true", timeout=2000)


async def test_backspace_deletes(page: Page) -> None:
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content('<input id="i" value="ab" autofocus>')
    await page.focus("#i")
    # Move caret to end, then delete one char.
    await page.eval_on_selector("#i", "el => el.setSelectionRange(2, 2)")

    await _key(server, cdp, "Backspace", "Backspace")

    assert await page.input_value("#i") == "a"


async def test_mouse_click_fires(page: Page) -> None:
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content(
        """
        <button id="b" style="position:fixed;left:0;top:0;width:200px;height:200px">x</button>
        <script>
          window.__clicked = false;
          document.getElementById('b').addEventListener('click', () => { window.__clicked = true; });
        </script>
        """
    )
    await server._handle_mouse(cdp, {"action": "mousedown", "x": 20, "y": 20, "button": 0})
    await server._handle_mouse(cdp, {"action": "mouseup", "x": 20, "y": 20, "button": 0})

    await page.wait_for_function("() => window.__clicked === true", timeout=2000)


async def test_context_menu_suppressed(page: Page) -> None:
    """The native right-click menu can't be shown over the stream, so the guard
    must cancel the contextmenu event on the page."""
    await page.set_content("<body>hi</body>")

    # Before installing the guard, the event is not prevented.
    before = await page.evaluate(
        "() => !document.body.dispatchEvent("
        "new MouseEvent('contextmenu', {bubbles: true, cancelable: true}))"
    )
    assert before is False

    await StreamingServer._suppress_context_menu(page)

    # After: dispatchEvent returns false because a handler called preventDefault.
    after = await page.evaluate(
        "() => !document.body.dispatchEvent("
        "new MouseEvent('contextmenu', {bubbles: true, cancelable: true}))"
    )
    assert after is True
