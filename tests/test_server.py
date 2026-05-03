"""Tests for server configuration."""

import pytest

from browser_handoff.server import ServerConfig


class TestServerConfig:
    """Tests for ServerConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = ServerConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 8080
        assert config.public_base is None
        assert config.timeout == 600.0

    def test_custom_values(self):
        """Test custom configuration values."""
        config = ServerConfig(
            host="localhost",
            port=3000,
            public_base="https://proxy.example.com",
            timeout=300.0,
        )
        assert config.host == "localhost"
        assert config.port == 3000
        assert config.public_base == "https://proxy.example.com"
        assert config.timeout == 300.0

    def test_get_base_url_with_public_base(self):
        """Test get_base_url with public_base set."""
        config = ServerConfig(
            host="localhost",
            port=8080,
            public_base="https://proxy.example.com/",
        )
        assert config.get_base_url() == "https://proxy.example.com"

    def test_get_base_url_without_public_base(self):
        """Test get_base_url without public_base."""
        config = ServerConfig(host="localhost", port=3000)
        assert config.get_base_url() == "http://localhost:3000"

    def test_to_dict(self):
        """Test serialization to dict."""
        config = ServerConfig(
            host="0.0.0.0",
            port=8080,
            public_base="https://example.com",
            timeout=120.0,
        )
        data = config.to_dict()
        assert data == {
            "host": "0.0.0.0",
            "port": 8080,
            "public_base": "https://example.com",
            "timeout": 120.0,
        }

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "host": "127.0.0.1",
            "port": 9000,
            "public_base": "https://proxy.test.com",
            "timeout": 60.0,
        }
        config = ServerConfig.from_dict(data)
        assert config.host == "127.0.0.1"
        assert config.port == 9000
        assert config.public_base == "https://proxy.test.com"
        assert config.timeout == 60.0

    def test_from_dict_defaults(self):
        """Test from_dict with missing values uses defaults."""
        config = ServerConfig.from_dict({})
        assert config.host == "0.0.0.0"
        assert config.port == 8080
        assert config.public_base is None
        assert config.timeout == 600.0

    def test_from_dict_partial(self):
        """Test from_dict with partial values."""
        config = ServerConfig.from_dict({"port": 5000})
        assert config.host == "0.0.0.0"
        assert config.port == 5000
        assert config.public_base is None
