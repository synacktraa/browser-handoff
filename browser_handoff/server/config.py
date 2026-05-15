"""Server configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServerConfig:
    """Configuration for the streaming server.

    Attributes:
        host: Host to bind the server to.
        port: Port to bind the server to.
        public_base: Public base URL for generating stream URLs (e.g., "https://proxy.example.com").
                     If not set, uses http://{host}:{port}.
        timeout: Timeout in seconds for waiting for human completion.
    """

    host: str = "0.0.0.0"
    port: int = 8080
    public_base: str | None = None
    timeout: float = 600.0
    jpeg_quality: int = 75
    every_nth_frame: int = 1

    def get_base_url(self) -> str:
        """Get the base URL for stream URLs."""
        if self.public_base:
            return self.public_base.rstrip("/")
        return f"http://{self.host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "host": self.host,
            "port": self.port,
            "public_base": self.public_base,
            "timeout": self.timeout,
            "jpeg_quality": self.jpeg_quality,
            "every_nth_frame": self.every_nth_frame,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerConfig":
        """Create from dictionary."""
        return cls(
            host=data.get("host", "0.0.0.0"),
            port=data.get("port", 8080),
            public_base=data.get("public_base"),
            timeout=data.get("timeout", 600.0),
            jpeg_quality=data.get("jpeg_quality", 75),
            every_nth_frame=data.get("every_nth_frame", 1),
        )
