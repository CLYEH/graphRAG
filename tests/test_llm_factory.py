"""Why: §3 freezes the LLM abstraction boundary — consumers hold a LlamaIndex
``LLM`` and the factory is the ONLY place a provider is named, configured
exclusively through core.config (never os.environ). Misconfiguration must
fail typed AT WIRING TIME, not minutes later on the first chunk's API call.
"""

from __future__ import annotations

import pytest
from llama_index.core.embeddings import BaseEmbedding
from pydantic import Field

from core.config import Settings
from core.llm import factory
from core.llm.factory import (
    LLMNotConfiguredError,
    _with_query_cache,
    chat_model,
    embedding_model,
    query_embedding_model,
)


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


class _CountingEmbedder(BaseEmbedding):
    """Drives the REAL ``BaseEmbedding.aget_text_embedding`` machinery — the
    method that consults ``embeddings_cache`` — and records every underlying
    embed, so a cache HIT is observable as a round-trip that never happened.
    (The other test fakes override the public method and thus bypass the cache
    hook; this one must NOT, so it implements only the abstract inners.)"""

    embedded: list[str] = Field(default_factory=list)

    def _get_text_embedding(self, text: str) -> list[float]:
        self.embedded.append(text)
        return [float(len(text))]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._get_text_embedding(query)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_text_embedding(query)


async def test_query_embedding_cache_skips_the_repeated_round_trip() -> None:
    """MCP18: the ~1s embedding round-trip is ~88% of `semantic` latency, and a
    museum guide asks the same few questions (ticket price, hours). The query
    embedder's cache must serve a REPEATED question from memory — the second
    identical embed does NOT reach the underlying model."""
    embedder = _CountingEmbedder(model_name="fake")
    _with_query_cache(embedder, 512)  # attaches the cache in place
    v1 = await embedder.aget_text_embedding("ticket price")
    v2 = await embedder.aget_text_embedding("ticket price")  # cache HIT
    assert v1 == v2
    assert embedder.embedded == ["ticket price"]  # underlying embed ran ONCE
    # a DIFFERENT question is a miss and does reach the model
    await embedder.aget_text_embedding("opening hours")
    assert embedder.embedded == ["ticket price", "opening hours"]


async def test_query_embedding_cache_evicts_least_recently_used() -> None:
    """The cache is BOUNDED (config `embedding_cache_size`): past capacity the
    least-recently-used entry is dropped, and re-asking it pays the round-trip
    again — the memory ceiling holds. A cache HIT also refreshes recency, so a
    kept-warm entry survives while a colder one is evicted."""
    embedder = _CountingEmbedder(model_name="fake")
    _with_query_cache(embedder, 2)  # attaches the cache in place
    await embedder.aget_text_embedding("a")  # [a]
    await embedder.aget_text_embedding("b")  # [a, b]
    await embedder.aget_text_embedding("a")  # HIT — refreshes a: [b, a]
    await embedder.aget_text_embedding("c")  # evicts LRU = b: [a, c]
    assert embedder.embedded == ["a", "b", "c"]  # only misses embedded
    await embedder.aget_text_embedding("a")  # still warm — HIT
    assert embedder.embedded == ["a", "b", "c"]
    await embedder.aget_text_embedding("b")  # evicted — re-embeds
    assert embedder.embedded == ["a", "b", "c", "b"]


async def test_query_embedding_cache_disabled_when_size_is_zero() -> None:
    """`embedding_cache_size == 0` disables the cache entirely (no
    ``embeddings_cache`` attached), so every call embeds — the escape hatch and
    exactly the ingestion path's behavior, which never calls this wrapper."""
    embedder = _CountingEmbedder(model_name="fake")
    _with_query_cache(embedder, 0)  # 0 → leaves the embedder uncached
    assert embedder.embeddings_cache is None
    await embedder.aget_text_embedding("x")
    await embedder.aget_text_embedding("x")  # no cache → embeds again
    assert embedder.embedded == ["x", "x"]


def test_query_embedding_model_attaches_the_cache_per_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`query_embedding_model` reads `embedding_cache_size` and wraps the SAME
    single-construction embedder — the query path gets the cache, and a 0 size
    leaves it uncached (both without demanding a key, since the model factory is
    faked here)."""
    monkeypatch.setattr(factory, "embedding_model", lambda: _CountingEmbedder(model_name="fake"))
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(embedding_cache_size=64))
    assert query_embedding_model().embeddings_cache is not None
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(embedding_cache_size=0))
    assert query_embedding_model().embeddings_cache is None
