"""Why: §3 freezes the LLM abstraction boundary — consumers hold a LlamaIndex
``LLM`` and the factory is the ONLY place a provider is named, configured
exclusively through core.config (never os.environ). Misconfiguration must
fail typed AT WIRING TIME, not minutes later on the first chunk's API call.
"""

from __future__ import annotations

import pytest

from core.config import Settings
from core.llm import factory
from core.llm.factory import LLMNotConfiguredError, chat_model, embedding_model


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "llm_provider": "openai",
        "llm_model": "gpt-5.4-nano",
        # the field's validation_alias replaces its init name too — construct
        # via the alias, exactly as the environment would provide it
        "OPENAI_API_KEY": "sk-test",
        "_env_file": None,  # isolate from the developer's real .env
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_unsupported_provider_fails_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(llm_provider="mystery"))
    with pytest.raises(LLMNotConfiguredError, match="unsupported llm_provider"):
        chat_model()


def test_missing_key_fails_at_wiring_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GRAPHRAG_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(**{"OPENAI_API_KEY": None}))
    with pytest.raises(LLMNotConfiguredError, match="OPENAI_API_KEY"):
        chat_model()


def test_openai_is_built_from_settings_with_deterministic_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model comes from settings (🔧 configurable), temperature is pinned 0 —
    extraction feeds fingerprint-deduped storage, so stability beats
    creativity."""
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(llm_model="gpt-x"))
    llm = chat_model()
    assert llm.model == "gpt-x"  # type: ignore[attr-defined]
    assert llm.temperature == 0.0  # type: ignore[attr-defined]


def test_embedding_missing_key_fails_at_wiring_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same wiring-time contract as chat_model: a missing key must fail when
    the index step is wired, not on the first chunk's embedding call."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GRAPHRAG_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(**{"OPENAI_API_KEY": None}))
    with pytest.raises(LLMNotConfiguredError, match="OPENAI_API_KEY"):
        embedding_model()


def test_embedding_is_built_from_settings_regardless_of_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3 names only OpenAI for embeddings — so the embedding model is built
    from the 🔧 embedding_model setting and the key ALONE; a non-OpenAI
    llm_provider (Claude for chat) must not block embeddings, which have no
    non-OpenAI provider to switch to."""
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: _settings(llm_provider="anthropic", embedding_model="text-embedding-3-small"),
    )
    embedder = embedding_model()
    assert embedder.model_name == "text-embedding-3-small"
