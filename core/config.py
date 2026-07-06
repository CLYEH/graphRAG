"""Central runtime configuration, loaded from environment / .env.

All service endpoints and LLM defaults live here so the rest of the code depends
on typed settings rather than reading os.environ directly. See DESIGN.md §3.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings for graphRAG services (env prefix ``GRAPHRAG_``)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="GRAPHRAG_", extra="ignore")

    # Stores (see DESIGN.md §2 — three-engine polyglot, Postgres = SoR)
    # host port 15432: the compose mapping avoids natively installed PostgreSQL on 5432
    postgres_dsn: str = "postgresql://graphrag:graphrag@localhost:15432/graphrag"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "graphrag-dev"
    qdrant_url: str = "http://localhost:6333"
    redis_url: str = "redis://localhost:6379/0"

    # §14 GC (tunable 🔧 retention.keep_builds): builds kept per project by
    # `graphrag prune` — the active build is always kept regardless
    retention_keep_builds: int = 5

    # LLM (default provider: OpenAI — DESIGN.md §3; abstraction = LlamaIndex LLM)
    llm_provider: str = "openai"
    llm_model: str = "gpt-5.4-nano"
    embedding_model: str = Field(default="text-embedding-3-large")
    # .env keeps the conventional unprefixed name (see .env.example); the
    # GRAPHRAG_-prefixed alias also works. Read here so no other module ever
    # touches os.environ for it (CLAUDE.md guardrail).
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "GRAPHRAG_OPENAI_API_KEY"),
    )


def get_settings() -> Settings:
    """Return freshly-loaded settings."""
    return Settings()
