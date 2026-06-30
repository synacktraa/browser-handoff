"""Server configuration."""

from __future__ import annotations

from dataclasses import dataclass
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
        access_timeout: Default seconds from `wait_for_completion()`
            call to first operator WS connect. Bounds orphan sessions.
            None disables this layer entirely.
        completion_timeout: Default seconds from first operator WS
            connect to detection match. Bounds the human's work budget.
            None disables this layer entirely.
        jpeg_quality: JPEG quality for screencast frames (1-100).
        every_nth_frame: Capture 1 of every N frames Chrome produces;
            higher values reduce CPU/bandwidth at the cost of smoothness.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    public_base: str | None = None
    access_timeout: float | None = 600.0
    completion_timeout: float | None = 1800.0
    jpeg_quality: int = 75
    every_nth_frame: int = 1

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
            "access_timeout": self.access_timeout,
            "completion_timeout": self.completion_timeout,
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
        if "access_timeout" in data:
            kwargs["access_timeout"] = data["access_timeout"]
        if "completion_timeout" in data:
            kwargs["completion_timeout"] = data["completion_timeout"]
        return cls(**kwargs)
