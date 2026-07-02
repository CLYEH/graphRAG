# TASKS — graphRAG build queue

The loop consumes the **top unchecked** item (respecting dependencies). One task per
iteration; definition of done = `uv run poe check-all` green, then commit. Protocol:
[`docs/LOOP.md`](docs/LOOP.md). Design: [`docs/DESIGN.md`](docs/DESIGN.md).

Mark `[x]` when merged & green. Keep items small enough to finish in one loop.

---

## ✅ Harness (done)
- [x] Repo skeleton (`core/ api/ cli/ web/`), `pyproject.toml`, uv env
- [x] Quality gates: ruff + mypy(strict) + pytest + poe; `poe check-all` (backend + frontend)
- [x] Frontend scaffold (React/Vite/TS) + oxlint/prettier/vitest
- [x] `docker-compose.yml` (postgres/neo4j/qdrant/redis)
- [x] Test tiers: unit/contract/integration/eval/e2e markers, `test-cov` (85%), conftest service-gating, Playwright scaffold
- [x] CI (`.github/workflows/ci.yml`: backend + coverage + integration + frontend), CLAUDE.md/AGENTS.md, `.env.example`

> **Per-task rule:** one task = one `task/<id>` branch = one PR. It lands with tests for its
> tier, passes local gates + the `code-reviewer` subagent, then merges only after CI **and**
> the bound Codex review are green (see [`docs/LOOP.md`](docs/LOOP.md)).

## Track 0 — Contracts & Governance  *(freeze BEFORE parallel work — DR-002)*
- [x] P0 `contracts/openapi.yaml`: response envelope, error-code enum, cursor pagination, SSE event, idempotency (§15/§27.2)
- [ ] P1 `contracts/mcp_response.schema.json`: unified retrieval result + source_refs + debug (§16/§27.2)
- [ ] P2 Build/activation model spec + Postgres migrations for `builds` + partial unique index (§14/§27.1)
- [ ] P3 Review state machine + `review_ledger` + fingerprint spec + `fingerprint_version` (§17/§27.3)
- [ ] P4 Eval contract: `golden.yaml` schema + metrics incl. path_validity/relation_hit_rate/groundedness (§20/§27.5)
- [ ] P5 Query safety policy schema (`query_policy`) + SQL(sqlglot)/Cypher strategy (§21/§27.6)
- [ ] P6 Observability schema: pipeline_runs/steps/items + item_ref rules (§18/§27.7)

## Track 1 — Core engine  *(depends on Track 0)*
- [ ] C1 Store adapters + **BuildScopedRepo** (build_id injection, DR-006) + PG migrations for all core tables
- [ ] C2 Ingest (structured + document connectors) + clean/chunking
- [ ] C3 Graph build (hybrid ontology extraction → entities/relations)
- [ ] C4 Entity resolution + apply `review_ledger`
- [ ] C5 Index: embeddings → Qdrant; project entities/relations → Neo4j
- [ ] C6 Retrieval: semantic / graph / sql / global + hybrid router (§8, §16 contract)
- [ ] C7 Global summary (Leiden communities + reports)
- [ ] C8 MCP server (per project) exposing the tool set
- [ ] C9 builds/activate/rollback/diff/prune (CLI + core)
- [ ] C10 Eval harness runner
- [ ] C11 Observability wiring + drift detection

## Track 2 — Console backend (FastAPI + arq)  *(needs Track 0 P0; C-items as they land)*
- [ ] BA0 API skeleton + generated OpenAPI matching contract + auth placeholder
- [ ] BA1 projects/sources endpoints + trigger ingest/build
- [ ] BA2 arq worker + jobs + SSE
- [ ] BA3 inspection endpoints (docs/chunks/entities/relations/subgraph/reports)
- [ ] BA4 cleaning preview/rules
- [ ] BA5 merge-candidate review endpoints
- [ ] BA6 query playground endpoints
- [ ] BA7 health/metrics
- [ ] BA8 builds/activate/rollback endpoints

## Track 3 — Console frontend (React)  *(needs BA0 contract; v1 = health/jobs/review/playground)*
- [ ] FE0 app shell + OpenAPI codegen client + project switcher
- [ ] FE7 Project Health home
- [ ] FE8 Pipeline/jobs dashboard
- [ ] FE5 Entity-resolution review UI
- [ ] FE6 Query playground UI
- [ ] FE1 Import · [ ] FE2 Clean · [ ] FE3 Inspect · [ ] FE4 Graph explorer  *(v2)*
