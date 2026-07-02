"""Central runtime configuration, loaded from environment / .env.

All service endpoints and LLM defaults live here so the rest of the code depends
on typed settings rather than reading os.environ directly. See DESIGN.md §3.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings for graphRAG services (env prefix ``GRAPHRAG_``)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="GRAPHRAG_", extra="ignore")

    # Stores (see DESIGN.md §2 — three-engine polyglot, Postgres = SoR)
    postgres_dsn: str = "postgresql://graphrag:graphrag@localhost:5432/graphrag"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "graphrag-dev"
    qdrant_url: str = "http://localhost:6333"
    redis_url: str = "redis://localhost:6379/0"

    # LLM (default provider: OpenAI — DESIGN.md §3, DR-005)
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    embedding_model: str = Field(default="text-embedding-3-large")


def get_settings() -> Settings:
    """Return freshly-loaded settings."""
    return Settings()
