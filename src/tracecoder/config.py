"""Typed environment configuration for TraceCoder."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when startup configuration is missing or invalid."""


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw_value = values.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuration loaded once at the CLI boundary."""

    api_key: str
    base_url: str
    model: str
    api_key_env_name: str
    max_steps: int = 20
    repeat_limit: int = 3
    context_max_chars: int = 100_000
    command_timeout_seconds: int = 60
    command_output_bytes: int = 20_000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Load and validate configuration from namespaced environment variables."""

        values = os.environ if environ is None else environ
        api_key_env_name = "TRACECODER_API_KEY" if values.get("TRACECODER_API_KEY") else "OPENAI_API_KEY"
        api_key = values.get(api_key_env_name, "").strip()
        if not api_key:
            raise ConfigError("TRACECODER_API_KEY (or OPENAI_API_KEY) is required")

        model = (values.get("TRACECODER_MODEL") or values.get("OPENAI_MODEL") or "").strip()
        if not model:
            raise ConfigError("TRACECODER_MODEL (or OPENAI_MODEL) is required")

        base_url = (
            values.get("TRACECODER_BASE_URL")
            or values.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip()
        if not base_url.startswith(("https://", "http://")):
            raise ConfigError("TRACECODER_BASE_URL must be an HTTP(S) URL")

        return cls(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            api_key_env_name=api_key_env_name,
            max_steps=_positive_int(values, "TRACECODER_MAX_STEPS", 20),
            repeat_limit=_positive_int(values, "TRACECODER_REPEAT_LIMIT", 3),
            context_max_chars=_positive_int(values, "TRACECODER_CONTEXT_MAX_CHARS", 100_000),
            command_timeout_seconds=_positive_int(values, "TRACECODER_COMMAND_TIMEOUT", 60),
            command_output_bytes=_positive_int(values, "TRACECODER_COMMAND_OUTPUT_BYTES", 20_000),
        )

