"""Server configuration."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServerConfig:
    """Streaming-server configuration.

    Immutable — a Handoff shares one config across runs, so build a new
    ServerConfig to change settings.

    Attributes:
        host: Bind address. "127.0.0.1" (default) is loopback-only;
            "0.0.0.0" exposes on the LAN.
        port: Port to bind to.
        public_base: Public base URL used in notification links
            (e.g. "https://my-tunnel.example.com"). Falls back to
            host:port, with wildcard binds rewritten to localhost.
        session_timeout: Max seconds a session may live — the human's
            budget and the lifetime of the stream-URL token.
        jpeg_quality: JPEG quality for screencast frames (1-100).
        every_nth_frame: Capture 1 of every N frames Chrome produces;
            higher values reduce CPU/bandwidth at the cost of smoothness.

    Deprecated:
        completion_timeout: old name for `session_timeout`; accepted
            with a DeprecationWarning and mirrored for read access.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    public_base: str | None = None
    session_timeout: float = 600.0
    # None means "not supplied"; __post_init__ reconciles with session_timeout.
    completion_timeout: float | None = None
    jpeg_quality: int = 75
    every_nth_frame: int = 1

    def __post_init__(self) -> None:
        # Frozen dataclass — assign through object.__setattr__.
        if self.completion_timeout is not None:
            warnings.warn(
                "ServerConfig.completion_timeout is deprecated; use "
                "session_timeout instead. It will be removed in a future "
                "major release.",
                DeprecationWarning,
                stacklevel=2,
            )
            object.__setattr__(self, "session_timeout", self.completion_timeout)
        # Mirror so code reading `config.completion_timeout` keeps working,
        # silently.
        object.__setattr__(self, "completion_timeout", self.session_timeout)

    def get_base_url(self) -> str:
        """Return the base URL for stream URLs.

        Wildcard binds (0.0.0.0, ::) are rewritten to `localhost` — they're
        bind addresses, not destinations.
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
        # Prefer the new key; fall back to the deprecated one (warns via
        # __post_init__).
        if "session_timeout" in data:
            kwargs["session_timeout"] = data["session_timeout"]
        elif "completion_timeout" in data:
            kwargs["completion_timeout"] = data["completion_timeout"]
        return cls(**kwargs)
