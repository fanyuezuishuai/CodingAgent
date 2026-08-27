"""Configuration contract tests."""

import pytest

from tracecoder.config import ConfigError, Settings


def test_settings_require_api_key() -> None:
    with pytest.raises(ConfigError, match="TRACECODER_API_KEY"):
        Settings.from_env({"TRACECODER_MODEL": "example-model"})


def test_settings_load_namespaced_environment() -> None:
    settings = Settings.from_env(
        {
            "TRACECODER_API_KEY": "secret-value",
            "TRACECODER_BASE_URL": "https://provider.example/v1",
            "TRACECODER_MODEL": "code-model",
            "TRACECODER_MAX_STEPS": "12",
        }
    )

    assert settings.api_key == "secret-value"
    assert settings.base_url == "https://provider.example/v1"
    assert settings.model == "code-model"
    assert settings.max_steps == 12


def test_settings_support_openai_fallback_names() -> None:
    settings = Settings.from_env(
        {
            "OPENAI_API_KEY": "fallback-secret",
            "OPENAI_BASE_URL": "https://gateway.example/v1",
            "OPENAI_MODEL": "fallback-model",
        }
    )

    assert settings.api_key == "fallback-secret"
    assert settings.model == "fallback-model"
    assert settings.api_key_env_name == "OPENAI_API_KEY"


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_settings_reject_invalid_positive_integers(value: str) -> None:
    with pytest.raises(ConfigError, match="TRACECODER_MAX_STEPS"):
        Settings.from_env(
            {
                "TRACECODER_API_KEY": "secret",
                "TRACECODER_MODEL": "model",
                "TRACECODER_MAX_STEPS": value,
            }
        )
