"""Smoke tests for core.config — also the harness's green baseline."""

from __future__ import annotations

from core.config import Settings, get_settings


def test_llm_defaults_to_openai() -> None:
    settings = Settings()
    assert settings.llm_provider == "openai"
    assert settings.embedding_model == "text-embedding-3-large"


def test_get_settings_returns_settings() -> None:
    assert isinstance(get_settings(), Settings)
