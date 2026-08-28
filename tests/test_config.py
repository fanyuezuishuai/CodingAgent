"""Configuration contract tests."""

from pathlib import Path

import pytest

from tracecoder.config import ConfigError, Settings
from tracecoder.runtime import build_agent


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


def test_settings_load_workspace_dotenv_without_mutating_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRACECODER_API_KEY=dotenv-secret\n"
        "TRACECODER_BASE_URL=https://api.deepseek.com\n"
        "TRACECODER_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    provided_environment: dict[str, str] = {}

    settings = Settings.from_env(provided_environment, env_file=env_file)

    assert settings.api_key == "dotenv-secret"
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.model == "deepseek-chat"
    assert provided_environment == {}


def test_environment_overrides_workspace_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRACECODER_API_KEY=file-secret\nTRACECODER_MODEL=file-model\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(
        {
            "TRACECODER_API_KEY": "environment-secret",
            "TRACECODER_MODEL": "environment-model",
        },
        env_file=env_file,
    )

    assert settings.api_key == "environment-secret"
    assert settings.model == "environment-model"


def test_settings_redact_dotenv_and_environment_api_key_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRACECODER_API_KEY=dotenv-tracecoder-secret\n"
        "OPENAI_API_KEY=dotenv-openai-secret\n"
        "TRACECODER_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    process_environment = {
        "TRACECODER_API_KEY": "environment-tracecoder-secret",
        "OPENAI_API_KEY": "environment-openai-secret",
        "TRACECODER_MODEL": "environment-model",
    }
    monkeypatch.setattr("tracecoder.runtime.OpenAICompatibleClient", lambda *_args: object())

    settings = Settings.from_env(process_environment, env_file=env_file)
    agent = build_agent(tmp_path, settings, lambda _argv, _workspace: True)
    secret_text = " ".join(
        [
            "dotenv-tracecoder-secret",
            "dotenv-openai-secret",
            "environment-tracecoder-secret",
            "environment-openai-secret",
        ]
    )

    assert settings.api_key == "environment-tracecoder-secret"
    assert settings.redaction_secrets == tuple(secret_text.split())
    assert agent.trace.redact(secret_text) == "[REDACTED] [REDACTED] [REDACTED] [REDACTED]"
    assert all(secret not in repr(settings) for secret in settings.redaction_secrets)


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
