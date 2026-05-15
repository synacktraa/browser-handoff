"""Server configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ServerConfig:
    """Configuration for the streaming server.

    Attributes:
        host: Bind address. Defaults to 127.0.0.1 (loopback). Set to
            "0.0.0.0" to expose on the LAN (e.g. for phone access or
            tunnel forwarding).
        port: Port to bind to.
        public_base: Public base URL used for notification links
            (e.g. "https://my-tunnel.example.com"). If unset, derived
            from host:port (with wildcard binds rewritten to localhost
            so the link is openable).
        completion_timeout: Max seconds to wait for the human to satisfy
            the scenario's `complete` condition before giving up.
        jpeg_quality: JPEG quality for screencast frames (1-100).
        every_nth_frame: Capture 1 of every N frames Chrome produces.
            Higher values reduce CPU/bandwidth at the cost of smoothness.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    public_base: str | None = None
    completion_timeout: float = 600.0
    jpeg_quality: int = 75
    every_nth_frame: int = 1

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
            "completion_timeout": self.completion_timeout,
            "jpeg_quality": self.jpeg_quality,
            "every_nth_frame": self.every_nth_frame,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerConfig":
        return cls(
            host=data.get("host", "127.0.0.1"),
            port=data.get("port", 8080),
            public_base=data.get("public_base"),
            completion_timeout=data.get("completion_timeout", 600.0),
            jpeg_quality=data.get("jpeg_quality", 75),
            every_nth_frame=data.get("every_nth_frame", 1),
        )
