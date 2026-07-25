"""LLM + embedding construction behind typed settings (DESIGN §3, C3b/C5).

§3 fixes the LLM abstraction: **LlamaIndex's built-in ``LLM`` base class**
(OpenAI + Claude switchable), default provider OpenAI 🔧 ``gpt-5.4-nano``.
This factory is the single place a concrete provider is constructed — every
consumer (extraction now, resolve/summarize later) takes an
``llama_index.core.llms.LLM`` and stays provider-blind, which IS the
switchability §3 promises. Configuration comes from :mod:`core.config` only;
no module reads ``os.environ`` (guardrail).

Temperature is pinned to 0: pipeline extraction feeds fingerprint-deduped
storage, so run-to-run stability matters more than creativity. Additional
providers (Claude per §3) are additive here — one new branch, consumers
untouched.

Embeddings ride the same boundary (C5's index step): §3 pins embeddings to
OpenAI ``text-embedding-3-large`` 🔧, and consumers hold a LlamaIndex
``BaseEmbedding`` so the provider stays swappable behind the abstraction.
§3 offers no non-OpenAI embedding provider (Claude has no embedding API), so
:func:`embedding_model` gates on the key only — but it is the SAME single
construction point, keyed through :mod:`core.config`, never ``os.environ``.
"""

from __future__ import annotations

from collections import OrderedDict

from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.llms import LLM

# llama-index-embeddings-openai ships no py.typed marker (its sibling llms
# package does), so mypy strict sees it as untyped — silence only this import;
# the return is re-typed to BaseEmbedding below so our own surface stays typed.
from llama_index.embeddings.openai import OpenAIEmbedding  # type: ignore[import-untyped]
from llama_index.llms.openai import OpenAI

from core.config import get_settings


class LLMNotConfiguredError(RuntimeError):
    """The configured provider cannot be constructed from settings.

    Raised at factory time — a missing key must fail when the pipeline is
    wired, not minutes later on the first chunk's API call.
    """


def chat_model() -> LLM:
    """Build the configured LLM (§3: provider 🔧, model 🔧, key via settings)."""
    settings = get_settings()
    if settings.llm_provider != "openai":
        raise LLMNotConfiguredError(
            f"unsupported llm_provider {settings.llm_provider!r} — 'openai' is "
            "the wired provider; adding one (e.g. Claude, §3) is an additive "
            "branch in core.llm.factory"
        )
    if not settings.openai_api_key:
        raise LLMNotConfiguredError(
            "OPENAI_API_KEY is not set — put it in .env (see .env.example); "
            "core reads it via core.config, never os.environ"
        )
    return OpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        temperature=0.0,
    )


def embedding_model() -> BaseEmbedding:
    """Build the configured embedding model (§3: OpenAI ``text-embedding-3-large`` 🔧).

    The index step (C5) embeds chunks + entities through this one point.
    Unlike :func:`chat_model` there is no provider branch: §3 names only
    OpenAI for embeddings (Claude exposes no embedding API), so a
    non-OpenAI ``llm_provider`` does not force a non-OpenAI embedder — the
    only precondition is the key, and a missing one fails typed AT WIRING
    TIME, not on the first chunk's API call.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise LLMNotConfiguredError(
            "OPENAI_API_KEY is not set — put it in .env (see .env.example); "
            "core reads it via core.config, never os.environ"
        )
    # re-type the untyped constructor result onto our own typed surface
    embedder: BaseEmbedding = OpenAIEmbedding(
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
    )
    return embedder


class _InMemoryEmbeddingCache:
    """Bounded in-memory LRU for QUERY embeddings (MCP18 query-latency).

    Plugged into a LlamaIndex ``BaseEmbedding`` through its built-in
    ``embeddings_cache`` hook: :meth:`BaseEmbedding.aget_text_embedding`
    consults this BEFORE the ~1s OpenAI round-trip, so a repeated question (a
    museum guide sees the same few) is served from memory and never hits the
    API. Riding the framework's own hook keeps its dispatcher/callback
    instrumentation intact — no method is overridden.

    A text embedding is a pure function of (model, text): the model is fixed
    per embedder instance, so keying by ``text`` alone is sufficient, and the
    result is BUILD-independent, so one cache per query-embedder INSTANCE is
    correct across builds (DR-006 does not apply — nothing cached here is
    build-scoped; the embedder outlives any single build). It is NOT process-
    global: each project's server holds its own (MCP16 shared bundle) and the
    API app holds one, so a multi-project gateway's aggregate ceiling is
    N × capacity. A single asyncio loop per server drives every consult, so the
    plain dict ops need no lock. The sync ``get``/``put`` mirror the async pair
    so the framework's (query-path-unused) sync surface can never AttributeError
    on a half-implemented cache.

    Pin note (load-bearing): the cache is attached by post-construction
    assignment to the ``embeddings_cache`` field (see :func:`_with_query_cache`),
    which bypasses the framework's ``BaseKVStore`` validator on that field. This
    relies on the pinned LlamaIndex — a bump that enables ``validate_assignment``
    or calls other ``BaseKVStore`` methods on the hook would break it; the
    real-machinery test in ``test_llm_factory`` pins the behavior so such a bump
    surfaces red rather than silently bypassing the cache.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._store: OrderedDict[tuple[str, str], dict[str, list[float]]] = OrderedDict()

    def get(self, key: str, collection: str) -> dict[str, list[float]] | None:
        hit = self._store.get((collection, key))
        if hit is not None:
            self._store.move_to_end((collection, key))
        return hit

    def put(self, key: str, val: dict[str, list[float]], collection: str) -> None:
        self._store[(collection, key)] = val
        self._store.move_to_end((collection, key))
        while len(self._store) > self._capacity:
            self._store.popitem(last=False)

    async def aget(self, key: str, collection: str) -> dict[str, list[float]] | None:
        return self.get(key, collection)

    async def aput(self, key: str, val: dict[str, list[float]], collection: str) -> None:
        self.put(key, val, collection)


def _with_query_cache(embedder: BaseEmbedding, cache_size: int) -> BaseEmbedding:
    """Attach the MCP18 query-embedding LRU to ``embedder`` when enabled.

    Pure and key-free (constructs no provider), so it is unit-testable against
    a fake embedder. ``cache_size <= 0`` leaves the embedder uncached — the
    contract the ingestion path relies on by NOT calling this."""
    if cache_size > 0:
        embedder.embeddings_cache = _InMemoryEmbeddingCache(cache_size)
    return embedder


def query_embedding_model() -> BaseEmbedding:
    """Build the embedding model for the QUERY path, wrapped in the MCP18
    bounded LRU (:func:`_with_query_cache`).

    Ingestion keeps the plain :func:`embedding_model` — its chunk/entity texts
    are distinct, so caching them only burns memory; only queries repeat (and
    each pays the ~1s round-trip otherwise). Same single construction point and
    key precondition as :func:`embedding_model`."""
    settings = get_settings()
    return _with_query_cache(embedding_model(), settings.embedding_cache_size)
