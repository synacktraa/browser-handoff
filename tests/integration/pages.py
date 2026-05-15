"""Crafted HTML pages served by the integration HTTP fixture.

Kept minimal — these exist for detection assertions, not visual rendering.
"""

from __future__ import annotations

_BASE = """\
<!doctype html>
<html><head>
<meta charset="utf-8">
<title>{title}</title>
</head><body>
{body}
</body></html>
"""


_LOGIN = _BASE.format(
    title="Sign In",
    body="""
<h1>Please log in</h1>
<form id="login-form" action="/dashboard" method="get">
  <input type="email" name="email" placeholder="email">
  <input type="password" name="password" placeholder="password">
  <button type="submit">Sign in</button>
</form>
""",
)

_DASHBOARD = _BASE.format(
    title="Dashboard",
    body="""
<h1>Welcome back</h1>
<nav class="user-menu">
  <a class="logout-button" href="/">Log out</a>
</nav>
""",
)

_PAYMENT = _BASE.format(
    title="Confirm Payment",
    body="""
<h1>Enter card details</h1>
<form class="payment-form">
  <input id="card-number" type="text" placeholder="1234 5678 9012 3456">
  <button type="submit">Confirm</button>
</form>
""",
)

_SPA_REDIRECT = _BASE.format(
    title="Loading",
    body="""
<h1>Loading...</h1>
<script>
  setTimeout(function() { window.location.href = '/login'; }, 300);
</script>
""",
)

# /dynamic exposes buttons that mutate the DOM. Every test that needs to
# trigger a MutationObserver event clicks one of these.
_DYNAMIC = _BASE.format(
    title="Dynamic",
    body="""
<h1>Dynamic content</h1>
<div id="container"></div>
<button id="add">Add</button>
<button id="remove">Remove</button>
<button id="hide">Hide all</button>
<script>
  const c = document.getElementById('container');
  document.getElementById('add').onclick = function() {
    const d = document.createElement('div');
    d.className = 'dynamic-item';
    d.textContent = 'item';
    c.appendChild(d);
  };
  document.getElementById('remove').onclick = function() {
    const items = c.querySelectorAll('.dynamic-item');
    if (items.length) c.removeChild(items[0]);
  };
  document.getElementById('hide').onclick = function() {
    c.querySelectorAll('.dynamic-item').forEach(function(el) {
      el.style.display = 'none';
    });
  };
</script>
""",
)

_EMPTY = _BASE.format(
    title="Empty",
    body="<h1>Empty page</h1>",
)


ROUTES: dict[str, str] = {
    "/": _EMPTY,
    "/login": _LOGIN,
    "/dashboard": _DASHBOARD,
    "/payment": _PAYMENT,
    "/spa-redirect": _SPA_REDIRECT,
    "/dynamic": _DYNAMIC,
}
