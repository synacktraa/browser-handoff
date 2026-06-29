"""Structured message items for notifiers.

Each notifier renders these natively (Rich markup, Discord embeds,
Slack mrkdwn, HTML <a>) instead of parsing a flat string.
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

    Notifiers render the URL natively (clickable hyperlink, OSC 8 in
    supported terminals, Discord embed `url`). Kept as one logical
    token so triple-click selection picks it up even when wrapped.
    """

    url: str
    prefix: str = ""
    suffix: str = ""


# typing.Union (not `|`) so get_args / isinstance helpers work uniformly.
MessageItem = Union[TextItem, LinkItem]


__all__ = ["TextItem", "LinkItem", "MessageItem"]
