# TASKS — graphRAG build queue

The loop consumes the **top unchecked** item (respecting dependencies). One task per
iteration; definition of done = `uv run poe check-all` green, then commit. Protocol:
[`docs/LOOP.md`](docs/LOOP.md). Design: [`docs/DESIGN.md`](docs/DESIGN.md).

Check an item off `[x]` inside its own task PR — the checkoff lands when that PR merges.
Keep items small enough to finish in one loop.

---

## Harness
- [x] Repo skeleton (`core/ api/ cli/ web/`), `pyproject.toml`, uv env
- [x] Quality gates: ruff + mypy(strict) + pytest + poe; `poe check-all` (backend + frontend)
- [x] Frontend scaffold (React/Vite/TS) + oxlint/prettier/vitest
- [x] `docker-compose.yml` (postgres/neo4j/qdrant/redis)
- [x] Test tiers: unit/contract/integration/eval/e2e markers, `test-cov` (85%), conftest service-gating, Playwright scaffold
- [x] CI (`.github/workflows/ci.yml`: backend + coverage + integration + frontend), CLAUDE.md/AGENTS.md, `.env.example`
- [x] H1 harness fixes: fail-loud CI integration gate (`--wait` + CI fail-not-skip), doc-drift cleanup (hook filename, push wording, P-numbering, checkoff rule), gate-wait pipelining in LOOP.md, reviewer model → opus, LLM default → `gpt-5.4-nano`, C1/C6 split, `gh`/`git` allowlist, CI dedupe/concurrency + qdrant pin, DR-008 (Alembic) recorded
- [x] H2 LOOP.md: Codex suggestion triage rules in step 7 (must-fix vs reply-and-resolve criteria, checkable rationale required for every resolve-without-change, same-class sweep per round; `+1` gate unchanged) + entry points aligned (CLAUDE.md gate 4, /loop prompt, memory)
- [x] H3 harness enforcement & efficiency: `scripts/watch-codex.sh` (standard 3-channel watcher) · doc-only fast lane (`docs/**` direct push, no PR/Codex — owner-approved; doc-reviewer subagent + push-gate hook + branch-protection relax) · CPU push gates (review receipts hash-bound + re-run checks) · CI `governance` job (DR-002 version-bump guard, TASKS.md checkoff lint)
- [ ] H4 property-based boundary tests: add `hypothesis` (dev dep) + property tests for frozen numeric/boundary rules (`is_eval_regression` first; P5 guardrail limits and C10 scoring as they land) — retro of PR #12's float-boundary must-fix (lesson class 8: 邊界語意 × 表示誤差)

> **Per-task rule:** one task = one `task/<id>` branch = one PR. It lands with tests for its
> tier, passes local gates + the `code-reviewer` subagent, then merges only after CI **and**
> the bound Codex review are green (see [`docs/LOOP.md`](docs/LOOP.md)).

## Track 0 — Contracts & Governance  *(freeze BEFORE parallel work — DR-002)*
- [x] P0 `contracts/openapi.yaml`: response envelope, error-code enum, cursor pagination, SSE event, idempotency (§15/§27.2)
- [x] P1 `contracts/mcp_response.schema.json`: unified retrieval result + source_refs + debug (§16/§27.2)
- [x] P2 Build/activation model spec + Postgres migrations for `builds` + partial unique index (§14/§27.1) · Alembic setup (DR-008)
- [x] P3 Review state machine + `review_ledger` + fingerprint spec + `fingerprint_version` (§17/§27.3)
- [x] P4 Eval contract: `golden.yaml` schema + metrics incl. path_validity/relation_hit_rate/groundedness (§20/§27.5)
- [ ] P5 Query safety policy schema (`query_policy`) + SQL(sqlglot)/Cypher strategy (§21/§27.6)
- [ ] P6 Observability schema: pipeline_runs/steps/items + item_ref rules (§18/§27.7)

## Track 1 — Core engine  *(depends on Track 0)*
- [ ] C1a PG migrations for core tables (documents/chunks/entities/relations/evidence/reports/merge_candidates/observability; `builds` landed with P2, `review_ledger` with P3)
- [ ] C1b **BuildScopedRepo** over Postgres (active-build lookup + build_id injection, DR-006)
- [ ] C1c Neo4j adapter + projection repo (build_id-filtered, DR-004)
- [ ] C1d Qdrant adapter + projection repo (build_id payload filter)
- [ ] C2 Ingest (structured + document connectors) + clean/chunking
- [ ] C3 Graph build (hybrid ontology extraction → entities/relations)
- [ ] C4 Entity resolution + apply `review_ledger`
- [ ] C5 Index: embeddings → Qdrant; project entities/relations → Neo4j
- [ ] C6a Retrieval: semantic (Qdrant kNN, §16 contract)
- [ ] C6b Retrieval: sql (NLSQL + sqlglot guardrail per P5, §27.6)
- [ ] C6c Retrieval: graph (parameterized Cypher templates + guardrail per P5, §27.6)
- [ ] C6d Retrieval: global (community_reports — needs C7)
- [ ] C6e Hybrid router + fusion + routing trace (§8, §16 debug)
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
