"""Tests for configuration loading."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from browser_handoff.config import interpolate_env_vars, load_file, load_json, load_yaml


class TestEnvVarInterpolation:
    """Tests for environment variable interpolation."""

    def test_simple_interpolation(self, monkeypatch):
        """Test simple ${VAR} interpolation."""
        monkeypatch.setenv("TEST_VAR", "hello")
        config = {"value": "${TEST_VAR}"}
        result = interpolate_env_vars(config)
        assert result["value"] == "hello"

    def test_multiple_vars_in_string(self, monkeypatch):
        """Test multiple variables in one string."""
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "8080")
        config = {"url": "http://${HOST}:${PORT}/api"}
        result = interpolate_env_vars(config)
        assert result["url"] == "http://localhost:8080/api"

    def test_nested_dict(self, monkeypatch):
        """Test interpolation in nested dicts."""
        monkeypatch.setenv("SECRET", "abc123")
        config = {
            "outer": {
                "inner": {
                    "key": "${SECRET}"
                }
            }
        }
        result = interpolate_env_vars(config)
        assert result["outer"]["inner"]["key"] == "abc123"

    def test_list_values(self, monkeypatch):
        """Test interpolation in lists."""
        monkeypatch.setenv("A", "first")
        monkeypatch.setenv("B", "second")
        config = {"items": ["${A}", "${B}", "literal"]}
        result = interpolate_env_vars(config)
        assert result["items"] == ["first", "second", "literal"]

    def test_missing_var_unchanged(self):
        """Test that missing vars are left unchanged."""
        config = {"value": "${NONEXISTENT_VAR_12345}"}
        result = interpolate_env_vars(config)
        assert result["value"] == "${NONEXISTENT_VAR_12345}"

    def test_non_string_values_unchanged(self):
        """Test that non-string values pass through."""
        config = {
            "number": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
        }
        result = interpolate_env_vars(config)
        assert result == config


class TestLoadJson:
    """Tests for JSON loading."""

    def test_load_json_string(self, monkeypatch):
        """Test loading JSON from string."""
        monkeypatch.setenv("TEST_HOST", "example.com")
        json_str = '{"host": "${TEST_HOST}", "port": 8080}'
        result = load_json(json_str)
        assert result["host"] == "example.com"
        assert result["port"] == 8080

    def test_load_json_complex(self, monkeypatch):
        """Test loading complex JSON structure."""
        monkeypatch.setenv("WEBHOOK", "https://hooks.slack.com/test")
        json_str = json.dumps({
            "trigger_on": [
                {"type": "content", "title_contains": ["Test"]}
            ],
            "notifiers": [
                {"type": "slack", "webhook_url": "${WEBHOOK}"}
            ]
        })
        result = load_json(json_str)
        assert result["notifiers"][0]["webhook_url"] == "https://hooks.slack.com/test"


class TestLoadYaml:
    """Tests for YAML loading."""

    def test_load_yaml_string(self, monkeypatch):
        """Test loading YAML from string."""
        monkeypatch.setenv("DB_HOST", "db.example.com")
        yaml_str = """
host: ${DB_HOST}
port: 5432
"""
        result = load_yaml(yaml_str)
        assert result["host"] == "db.example.com"
        assert result["port"] == 5432

    def test_load_yaml_nested(self, monkeypatch):
        """Test loading nested YAML."""
        monkeypatch.setenv("API_KEY", "secret123")
        yaml_str = """
server:
  host: localhost
  port: 8080
auth:
  api_key: ${API_KEY}
"""
        result = load_yaml(yaml_str)
        assert result["server"]["host"] == "localhost"
        assert result["auth"]["api_key"] == "secret123"


class TestLoadFile:
    """Tests for file loading."""

    def test_load_json_file(self, monkeypatch, tmp_path):
        """Test loading a .json file."""
        monkeypatch.setenv("FILE_TEST", "json_value")
        config_file = tmp_path / "config.json"
        config_file.write_text('{"key": "${FILE_TEST}"}')

        result = load_file(config_file)
        assert result["key"] == "json_value"

    def test_load_yaml_file(self, monkeypatch, tmp_path):
        """Test loading a .yaml file."""
        monkeypatch.setenv("YAML_TEST", "yaml_value")
        config_file = tmp_path / "config.yaml"
        config_file.write_text("key: ${YAML_TEST}")

        result = load_file(config_file)
        assert result["key"] == "yaml_value"

    def test_load_yml_file(self, monkeypatch, tmp_path):
        """Test loading a .yml file."""
        monkeypatch.setenv("YML_TEST", "yml_value")
        config_file = tmp_path / "config.yml"
        config_file.write_text("key: ${YML_TEST}")

        result = load_file(config_file)
        assert result["key"] == "yml_value"

    def test_load_file_not_found(self, tmp_path):
        """Test FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_file(tmp_path / "nonexistent.json")

    def test_load_file_unsupported_format(self, tmp_path):
        """Test ValueError for unsupported file format."""
        config_file = tmp_path / "config.txt"
        config_file.write_text("some content")

        with pytest.raises(ValueError, match="Unsupported configuration file format"):
            load_file(config_file)

    def test_load_file_path_as_string(self, monkeypatch, tmp_path):
        """Test loading with path as string."""
        monkeypatch.setenv("STR_PATH_TEST", "string_path")
        config_file = tmp_path / "config.json"
        config_file.write_text('{"value": "${STR_PATH_TEST}"}')

        result = load_file(str(config_file))
        assert result["value"] == "string_path"
