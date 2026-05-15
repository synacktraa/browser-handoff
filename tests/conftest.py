"""Top-level pytest hooks."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Run unit tests before integration tests.

    Integration tests pay a chromium-launch cost on first use; putting
    them last means a unit-test failure surfaces in seconds rather than
    after the browser warm-up. Stable sort preserves intra-group order.
    """
    items.sort(
        key=lambda item: "integration" in item.nodeid.split("::")[0].split("/")
    )
