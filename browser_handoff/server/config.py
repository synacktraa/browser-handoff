"""Server configuration."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServerConfig:
    """Configuration for the streaming server.

    Immutable: a Handoff reuses one config across runs and shares it with the
    streaming server, so values are fixed at construction. Build a new
    ServerConfig to change settings.

    Attributes:
        host: Bind address. Defaults to 127.0.0.1 (loopback). Set to
            "0.0.0.0" to expose on the LAN (e.g. for phone access or
            tunnel forwarding).
        port: Port to bind to.
        public_base: Public base URL used for notification links
            (e.g. "https://my-tunnel.example.com"). If unset, derived
            from host:port (with wildcard binds rewritten to localhost
            so the link is openable).
        session_timeout: Max seconds a handoff session may live — the budget
            for the human to satisfy the scenario's `complete` condition, and
            the lifetime of the session's stream-URL token.
        jpeg_quality: JPEG quality for screencast frames (1-100).
        every_nth_frame: Capture 1 of every N frames Chrome produces.
            Higher values reduce CPU/bandwidth at the cost of smoothness.

    Deprecated:
        completion_timeout: old name for `session_timeout`. Still accepted
            (with a DeprecationWarning) and readable as a mirror; will be
            removed in a future major release.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    public_base: str | None = None
    session_timeout: float = 600.0
    # Deprecated alias for session_timeout. Kept as a field so it can be both
    # passed to the constructor (with a warning) and read by old code; None
    # means "not supplied", which __post_init__ reconciles below.
    completion_timeout: float | None = None
    jpeg_quality: int = 75
    every_nth_frame: int = 1

    def __post_init__(self) -> None:
        # frozen dataclass → assign through object.__setattr__.
        if self.completion_timeout is not None:
            warnings.warn(
                "ServerConfig.completion_timeout is deprecated; use "
                "session_timeout instead. It will be removed in a future "
                "major release.",
                DeprecationWarning,
                stacklevel=2,
            )
            object.__setattr__(self, "session_timeout", self.completion_timeout)
        # Mirror the canonical value onto the alias so code that still reads
        # `config.completion_timeout` keeps working — without a warning.
        object.__setattr__(self, "completion_timeout", self.session_timeout)

    def get_base_url(self) -> str:
        """Get the base URL for stream URLs.

        When `host` is a wildcard bind (0.0.0.0 or ::), the URL is rewritten
        to use `localhost` so the link is openable from a browser. Wildcards
        are bind addresses, not destinations.
        """
        if self.public_base:
            return self.public_base.rstrip("/")
        host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        return f"http://{host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "public_base": self.public_base,
            "session_timeout": self.session_timeout,
            "jpeg_quality": self.jpeg_quality,
            "every_nth_frame": self.every_nth_frame,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerConfig":
        kwargs: dict[str, Any] = dict(
            host=data.get("host", "127.0.0.1"),
            port=data.get("port", 8080),
            public_base=data.get("public_base"),
            jpeg_quality=data.get("jpeg_quality", 75),
            every_nth_frame=data.get("every_nth_frame", 1),
        )
        # Prefer the new key; fall back to the deprecated one (which routes
        # through __post_init__ and emits the warning).
        if "session_timeout" in data:
            kwargs["session_timeout"] = data["session_timeout"]
        elif "completion_timeout" in data:
            kwargs["completion_timeout"] = data["completion_timeout"]
        return cls(**kwargs)
