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


async def test_paste_inserts_text_into_focused_input(page: Page) -> None:
    """`_handle_paste` drops the operator's clipboard text at the page's focus
    via `Input.insertText`. The remote browser's own clipboard is bypassed —
    that's the whole point of the relay."""
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content('<input id="i" autofocus>')
    await page.focus("#i")

    await server._handle_paste(cdp, {"text": "hello world"})

    assert await page.input_value("#i") == "hello world"


async def test_paste_inserts_into_contenteditable(page: Page) -> None:
    """insertText routes to the focused element regardless of type, so
    contenteditable surfaces (rich editors, chat boxes) work too."""
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content('<div id="d" contenteditable autofocus></div>')
    await page.focus("#d")

    await server._handle_paste(cdp, {"text": "pasted"})

    assert (await page.text_content("#d")) == "pasted"


async def test_paste_with_empty_text_is_noop(page: Page) -> None:
    """An empty paste payload must not call CDP and must not throw."""
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content('<input id="i" value="keep" autofocus>')
    await page.focus("#i")
    await page.eval_on_selector("#i", "el => el.setSelectionRange(4, 4)")

    await server._handle_paste(cdp, {"text": ""})
    await server._handle_paste(cdp, {})  # missing key entirely

    assert await page.input_value("#i") == "keep"


async def test_read_selection_returns_selected_text(page: Page) -> None:
    """`_read_selection` reads the remote page's selection — the text the
    server then sends back to the operator's local clipboard."""
    await page.set_content("<p id='p'>hello clipboard world</p>")
    # Select "clipboard" inside the <p>.
    await page.evaluate(
        """() => {
            const node = document.getElementById('p').firstChild;
            const range = document.createRange();
            range.setStart(node, 6);
            range.setEnd(node, 15);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        }"""
    )

    assert (await StreamingServer._read_selection(page)) == "clipboard"


async def test_read_selection_empty_when_nothing_selected(page: Page) -> None:
    await page.set_content("<p>hi</p>")
    assert (await StreamingServer._read_selection(page)) == ""


async def test_drag_selects_text(page: Page) -> None:
    """Holding the left button while moving the mouse must extend a text
    selection on the remote. Without the buttons bitmask reaching CDP,
    moves are treated as hovers and the drag selects nothing."""
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content(
        "<p id='p' style='font-size:24px;font-family:monospace'>"
        "hello world handoff</p>"
    )
    box = await page.locator("#p").bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    x_start = box["x"] + 2
    x_end = box["x"] + box["width"] - 2

    await server._handle_mouse(
        cdp,
        {"action": "mousedown", "x": x_start, "y": y, "button": 0, "clickCount": 1},
    )
    # Several intermediate moves with left button held (buttons=1) so the
    # remote sees a real drag, not a teleport from mousedown to mouseup.
    steps = 10
    for i in range(1, steps + 1):
        xi = x_start + (x_end - x_start) * i / steps
        await server._handle_mouse(
            cdp, {"action": "mousemove", "x": xi, "y": y, "buttons": 1}
        )
    await server._handle_mouse(
        cdp,
        {"action": "mouseup", "x": x_end, "y": y, "button": 0, "clickCount": 1},
    )

    selected = await StreamingServer._read_selection(page)
    assert selected, "drag should produce a non-empty selection on the remote"


async def test_double_click_selects_word(page: Page) -> None:
    """clickCount must propagate so a double-click selects the word at the
    click point — without it the remote sees two separate single-clicks."""
    server = StreamingServer()
    cdp = await _cdp(page)
    await page.set_content("<p id='p' style='font-size:24px'>handoff</p>")
    box = await page.locator("#p").bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    await server._handle_mouse(cdp, {"action": "mousedown", "x": x, "y": y, "button": 0, "clickCount": 1})
    await server._handle_mouse(cdp, {"action": "mouseup", "x": x, "y": y, "button": 0, "clickCount": 1})
    await server._handle_mouse(cdp, {"action": "mousedown", "x": x, "y": y, "button": 0, "clickCount": 2})
    await server._handle_mouse(cdp, {"action": "mouseup", "x": x, "y": y, "button": 0, "clickCount": 2})

    assert (await StreamingServer._read_selection(page)) == "handoff"


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
