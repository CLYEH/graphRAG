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

    # §20 eval gate (tunable 🟡 eval.regression_threshold): a candidate whose
    # eval score falls below the active build's by MORE than this blocks
    # activation
    eval_regression_threshold: float = 0.05

    # §18 observability (tunable 🔧): which pipeline item ROWS persist —
    # failures (default; the §27.7 retry boundary's frozen minimum) |
    # sampled | all. Counters are always complete regardless.
    observability_item_logging: str = "failures"
    # §18 retention (🔧): per-item detail older than this is purged; runs
    # and step counters are kept
    observability_item_retention_days: int = 30

    # §27 idempotency (tunable 🔧): how long a stored Idempotency-Key response
    # is replayed before the key expires; a later reuse past this is a fresh
    # request, not a replay
    idempotency_ttl_hours: int = 24

    # §5/§22 pipeline step abort threshold (tunable 🔧): a build step aborts the
    # run when its FAILED-item ratio exceeds this (0.5 → abort once more than
    # half of a step's items fail). DESIGN §5/§22 leaves "failed_count > 閾值"
    # unquantified; this is a concrete, reversible default (BA2c) — flagged for
    # owner sign-off, changeable without a contract bump (not a frozen deliverable).
    pipeline_step_failure_ratio: float = 0.5

    # BA2d worker (tunable 🔧): arq's per-job timeout AND — because arq keeps its
    # in-progress key for job_timeout+10s — the ceiling on how long a crashed
    # worker's build stays un-redispatchable. Kept modest (not the whole-build
    # runtime) so a crash reclaims in minutes, not an hour: a build that outruns
    # it is killed and resumed at STAGE granularity (each committed stage is
    # skipped on the next arq try, and the DB lease guarantees that retry finds a
    # free lease). So the bound is the longest single STAGE, not the whole build —
    # a stage that alone exceeds it is rolled back and re-run every try, never
    # converging. Raise it for slow corpora (accepting slower crash recovery).
    build_job_timeout_seconds: int = 600

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
