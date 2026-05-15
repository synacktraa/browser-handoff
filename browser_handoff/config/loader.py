"""Configuration loading with JSON/YAML support and env var interpolation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def interpolate_env_vars(config: Any) -> Any:
    """Recursively interpolate ${VAR} patterns with environment variables.

    Args:
        config: Configuration dict, list, or value to interpolate.

    Returns:
        The interpolated configuration.

    Example:
        config = {"url": "${BASE_URL}/api"}
        result = interpolate_env_vars(config)
        # If BASE_URL=https://example.com, result is {"url": "https://example.com/api"}
    """
    pattern = re.compile(r"\$\{([^}]+)\}")

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            return pattern.sub(
                lambda m: os.environ.get(m.group(1), m.group(0)), value
            )
        elif isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [replace(v) for v in value]
        return value

    return replace(config)


def load_json(content: str) -> dict[str, Any]:
    """Load configuration from JSON string.

    Args:
        content: JSON string content.

    Returns:
        Parsed and interpolated configuration dict.
    """
    config = json.loads(content)
    return interpolate_env_vars(config)


def load_yaml(content: str) -> dict[str, Any]:
    """Load configuration from YAML string.

    Args:
        content: YAML string content.

    Returns:
        Parsed and interpolated configuration dict.

    Raises:
        ImportError: If PyYAML is not installed.
    """
    import yaml

    config = yaml.safe_load(content)
    return interpolate_env_vars(config)


def load_file(path: str | Path) -> dict[str, Any]:
    """Load configuration from a JSON or YAML file.

    Args:
        path: Path to the configuration file.

    Returns:
        Parsed and interpolated configuration dict.

    Raises:
        ValueError: If file extension is not .json, .yaml, or .yml.
        FileNotFoundError: If file does not exist.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    content = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix == ".json":
        return load_json(content)
    elif suffix in (".yaml", ".yml"):
        return load_yaml(content)
    else:
        raise ValueError(
            f"Unsupported configuration file format: {suffix}. "
            "Use .json, .yaml, or .yml"
        )
