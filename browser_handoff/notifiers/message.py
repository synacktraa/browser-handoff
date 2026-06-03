"""Structured message items for notifiers.

Notifiers receive a list of these in `Notifier.send(message=...)` and each
channel renders them with its native primitives — Rich link markup for
the console, embed fields for Discord, mrkdwn hyperlinks for Slack,
<a href> tags for HTML email — instead of a pre-formatted string that
every channel has to parse back out.

A plain `str` is still accepted for back-compat; the base class wraps it
as a single TextItem so subclasses always operate on the structured form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class TextItem:
    """A paragraph of plain prose."""

    text: str


@dataclass(frozen=True)
class LinkItem:
    """A URL with optional prefix/suffix labels.

    Notifiers render the link natively (clickable hyperlink in Slack/email,
    OSC 8 link in supported terminals, embed `url` field on Discord). The
    URL is kept as one logical token so triple-click selection picks it up
    in the console even when the line wraps visually.
    """

    url: str
    prefix: str = ""
    suffix: str = ""


# Use the Union syntax (not `|`) because the typing module's get_args /
# isinstance helpers work on Union[...] across all supported Pythons.
MessageItem = Union[TextItem, LinkItem]


__all__ = ["TextItem", "LinkItem", "MessageItem"]
