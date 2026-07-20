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

    # BA2d worker (tunable 🔧): arq's per-job timeout — a GENEROUS backstop against
    # a truly hung build, NOT the crash-recovery timer. arq cancels a job that
    # outruns this via asyncio.wait_for and does NOT retry the resulting
    # TimeoutError, so a cancel mid-stage would strand the SoR jobs row non-terminal
    # (stuck 'running'); it must therefore exceed any legitimately-slow build.
    # Crash recovery is deliberately DECOUPLED from this: the BA2d-3 lease reaper
    # re-enqueues a `building` build whose heartbeat-lease has expired within ~a
    # minute, regardless of job_timeout. Lower only if you also want a tighter
    # hung-build backstop (and accept the SoR-stranding risk for builds that slow).
    build_job_timeout_seconds: int = 86_400  # 24h

    # BA2e trigger crash-window sweep (tunable 🔧): a `queued` job that has never
    # acquired an execution lease and is older than this grace is re-enqueued by
    # the reaper cron — it should long since have been dispatched, so its arq
    # entry was lost (trigger crashed after commit, Redis lost the enqueue, or a
    # dispatch raced the trigger's commit and no-opped). Generous on purpose: a
    # job legitimately waiting in a backlogged queue also matches, where the
    # re-enqueue under the job's own arq id is dedup-refused (a harmless no-op),
    # so the only cost of a small value is reaper chatter, not duplicate builds.
    job_enqueue_grace_seconds: int = 120

    # BA2e-2 SSE (tunable 🔧): how often GET /jobs/{id}/events re-reads the jobs
    # SoR row between emitted frames. Lower = snappier progress at more DB
    # round-trips per open stream; each poll is a short-lived single-row
    # indexed SELECT on its own connection.
    sse_poll_interval_seconds: float = 0.5

    # C8b MCP HTTP transport (tunable 🔧, DESIGN §9: transport stdio/http):
    # where the per-project MCP server binds when run with --transport http
    # (streamable HTTP). 127.0.0.1 by default — exposing beyond localhost is a
    # deliberate operator opt-in (§23 auth is still a placeholder; see
    # graphrag-goal-museum-guide for the external-platform use case driving
    # HTTP support). NOTE: a non-localhost host also drops the SDK's
    # DNS-rebinding protection (auto-enabled only for localhost bindings), so
    # wider exposure has NO transport-layer guard until §23 auth lands.
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = 8300

    # The host an EXTERNAL agent should dial, when that differs from the bind
    # interface above. A bind is an interface, not an address: `0.0.0.0` / `::`
    # mean "every interface" and are meaningless to a client — advertising them
    # hands the operator a URL their agent resolves LOCALLY and never reaches
    # this gateway (Codex #113 P1). Set this to the machine's LAN name/IP when
    # binding a wildcard, AND whenever a reverse proxy rewrites ``Host``:
    # left unset, the MCP info endpoint derives the host from the bind, and for
    # an unspecified bind falls back to the CLIENT-SUPPLIED ``Host`` header the
    # Console was reached on (the same machine in the DR-012 single-host
    # deploy) — behind a rewriting proxy that header is no longer this host.
    mcp_public_host: str | None = None

    # UXC1b uploads (tunable 🔧, DR-010): the server-managed corpus root — each
    # project's uploaded files live under ``<upload_corpus_dir>/<project>/`` and a
    # single canonical file:// source points at that directory. Assumes the API
    # and the build worker share a filesystem (single-host deploy); a distributed
    # deploy would swap this for object storage behind the same source uri.
    upload_corpus_dir: str = "data/uploads"
    # Per-file size ceiling — a single file above this is a STATED per-file
    # rejection (manifest row), not a request failure (DR-010).
    upload_max_file_bytes: int = 10_000_000
    # Whole-request size ceiling — a multipart body whose total exceeds this is
    # refused with 413 before any file is stored.
    upload_max_total_bytes: int = 50_000_000

    # UXC1b eval (tunable 🔧, DR-010): root of the per-project on-disk files the
    # eval job reads — since CFG1/DR-012 the ONLY file input is the golden set
    # (``<projects_dir>/<project>/eval/golden.yaml``; the query policy lives in
    # the ``projects.config`` registry), the same path the CLI ``eval`` walks.
    projects_dir: str = "projects"

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
