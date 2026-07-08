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
- [x] H4 property-based boundary tests: add `hypothesis` (dev dep) + property tests for frozen numeric/boundary rules (`is_eval_regression` first; P5 guardrail limits and C10 scoring as they land) — retro of PR #12's float-boundary must-fix (lesson class 8: 邊界語意 × 表示誤差)
- [x] H5 per-branch review receipts: key `.claude/receipts/review` by branch (or one receipt file per tree hash) so parallel `task/*` + `docs/*` work stops overwriting each other's PASS stamps (push gate fail-closes correctly today but forces a reviewer re-stamp round-trip every time) — touch `write-review-receipt.sh` + `require-push-gates.sh` + `tests/test_receipts.py` — retro of PR #15 (receipt slot collision during gate-wait pipelining)
- [x] H8 watcher poke→watch race fix + quota probe loop: `watch-codex.sh` bootstrap scans BACKWARD at startup (supersession rule: the latest quota message governs unless a newer +1/review/poke exists) so a limits reply landing seconds after a poke — before the watcher starts — no longer polls blind to timeout; `probe-codex.sh` pokes/classifies/sleeps in rounds for quota-outage recovery — owner-reported miss during PR #28
- [x] H7 quota-aware Codex watcher: `watch-codex.sh` exit `30` when the only fresh bot response is the "reached your Codex usage limits" comment (checked before the exit-10 triage verdict) — stop waiting on an exhausted quota and re-poke after the reset time; LOOP.md step-6 exit-code doc updated — owner-directed during PR #25's quota outage
- [x] H6 migration `0005`: `pipeline_step_items.item_ref <> ''` CHECK (+ `item_kind <> ''`) — the identifier-non-empty rule applied to the P6 table it predates; an empty item_ref is a no-op identity in the §27.7 retry dedup — retro of PR #17 (deferred there as out-of-scope for an already-merged table)
- [x] H9 loop-throughput fixes (3 bottlenecks; all `*.md` → doc fast lane): the dominant cost is the external Codex round-trip, so (1) **cut rounds** — fold PR #47's config/parser completeness-matrix (input-position × level) + lenient-only-at-contract-boundary lessons into `code-reviewer.md` §7, and frame LOOP step 5 as the pre-push adversarial pass that runs the matching §6/§7 matrix to COMPLETION (round spent locally, not on Codex); (2) **batch re-stamp** — LOOP step 7 makes explicit the re-review/re-stamp is once per Codex round (after the same-class sweep), not per finding; (3) **overlap gate-wait** — already codified (LOOP step 7 "don't idle while gates run"), reinforced. Retro of PR #47's 2-round Codex (both findings one class a position apart — one matrix pass covers both)

> **Per-task rule:** one task = one `task/<id>` branch = one PR. It lands with tests for its
> tier, passes local gates + the `code-reviewer` subagent, then merges only after CI **and**
> the bound Codex review are green (see [`docs/LOOP.md`](docs/LOOP.md)).

## Track 0 — Contracts & Governance  *(freeze BEFORE parallel work — DR-002)*
- [x] P0 `contracts/openapi.yaml`: response envelope, error-code enum, cursor pagination, SSE event, idempotency (§15/§27.2)
- [x] P1 `contracts/mcp_response.schema.json`: unified retrieval result + source_refs + debug (§16/§27.2)
- [x] P2 Build/activation model spec + Postgres migrations for `builds` + partial unique index (§14/§27.1) · Alembic setup (DR-008)
- [x] P3 Review state machine + `review_ledger` + fingerprint spec + `fingerprint_version` (§17/§27.3)
- [x] P4 Eval contract: `golden.yaml` schema + metrics incl. path_validity/relation_hit_rate/groundedness (§20/§27.5)
- [x] P5 Query safety policy schema (`query_policy`) + SQL(sqlglot)/Cypher strategy (§21/§27.6)
- [x] P6 Observability schema: pipeline_runs/steps/items + item_ref rules (§18/§27.7)

## Track 1 — Core engine  *(depends on Track 0)*
- [x] C1a PG migrations for core tables (documents/chunks/entities/relations/evidence/reports/merge_candidates; `builds` landed with P2, `review_ledger` with P3, observability with P6)
- [x] C1b **BuildScopedRepo** over Postgres (active-build lookup + build_id injection, DR-006)
- [x] C1c Neo4j adapter + projection repo (build_id-filtered, DR-004)
- [x] C1d Qdrant adapter + projection repo (build_id payload filter)
- [x] C2 Ingest (structured + document connectors) + clean/chunking
- [x] C3a Graph build — structured rule-mapping extraction → entities/mentions/relations/evidence (deterministic, no LLM)
- [x] C3b Graph build — LLM document extraction (schema-guided, §27.4 quote-span evidence) + LLM factory (§3: LlamaIndex 抽象)
- [x] C3c Ontology proposal pool (LLM-proposed new types + `ontology.proposal_policy`)
- [x] C4 Entity resolution + apply `review_ledger`
- [x] C5 Index: embeddings → Qdrant; project entities/relations → Neo4j
- [x] C6a Retrieval: semantic (Qdrant kNN, §16 contract)
- [x] C6b Retrieval: sql (NLSQL + sqlglot guardrail per P5, §27.6)
- [x] C6c Retrieval: graph (parameterized Cypher templates + guardrail per P5, §27.6)
- [x] C6d Retrieval: global (community_reports — needs C7)
- [x] C6e Hybrid router + fusion + routing trace (§8, §16 debug)
- [x] C7 Global summary (Leiden communities + reports)
- [x] C8 MCP server (per project) exposing the tool set
- [x] C9 builds/activate/rollback/diff/prune (CLI + core)
- [x] C10 Eval harness runner
- [x] C11 Observability wiring + drift detection

## Track 2 — Console backend (FastAPI + arq)  *(needs Track 0 P0; C-items as they land)*
- [x] BA0 API skeleton + generated OpenAPI matching contract + auth placeholder
- [x] BA1a projects/sources registry — schema + core CRUD
- [x] BA1b projects/sources endpoints — routers + idempotency + opaque cursor
- [x] BA2a jobs table + core job repo + delete-project active-jobs guard
- [x] BA2b builds→projects FK RESTRICT + fixture sweep (close the delete TOCTOU structurally)
- [x] BA2c-1 registry-aware build creation + pipeline orchestrator control flow (six §5 stages injected as a seam; step recording, §22 abort, cooperative cancel, resume; fake stages, hermetic Postgres-only tests)
- [x] BA2c-2a build-config loader — `projects.config` JSONB → typed `TextOntology`/`StructuredMapping`/`ResolutionConfig`/chunk params (reuse dataclass validation, no frozen contract; lenient top-level, strict leaves; unit-tested)
- [x] BA2c-2b sources→connector resolution + `default_stages` + the six stage adapters (shared-conn writer/projectors); component (shared-conn spy) + integration (real stores, fake LLM/embedder) tests
- [x] BA2c-2c two-lane real-LLM test — hermetic + real `chat_model()`/`embedding_model()` over a tiny corpus, key-gated skip-only (no CI secret)
- [x] BA2d-1 **execution lease** (Codex BA2c-1 P2, DB heartbeat-lease): `jobs.lease_owner`/`lease_expires_at` + acquire/renew/release primitives (atomic conditional UPDATE, DB-clock expiry — the guard lives in the write) + `run_build_leased` wrapper (heartbeat renews while run_build runs, release on exit incl. failure, a crashed holder's expired lease is reclaimed by the next dispatch). Closes the gap that BA2c-1's FOR UPDATE lock serializes build *creation* but not concurrent *execution* of the same building build; the lease is a liveness layer over the convergent-idempotency safety floor. Component (fast-lane) + concurrency/reclaim integration tests.
- [x] BA2d-2 arq worker + Redis wiring — `arq` dep + `WorkerSettings` (Redis pool from `settings.redis_url`, on_startup/shutdown dep bundle à la MCP lifespan) + a `@func` task that runs `run_build_leased` + enqueue helper (`_job_id=job_id` for arq's own dispatch dedup). Real-worker integration test. (BA2e adds the HTTP trigger that create_job + enqueues.)
- [x] BA2d-3 **lease reaper** — decouple crash recovery from arq's `job_timeout` (Codex BA2d-2 P1): with a generous job_timeout (so arq never cancels a live build → no stranded SoR row), a periodic arq cron (`reap_stuck_builds`, 2×/min, `unique=True`) sweeps jobs whose heartbeat-lease has expired while non-terminal (crashed/starved worker) and re-enqueues them under a fresh arq id (no `_job_id`) so `run_build_leased` re-acquires the now-free lease and resumes — fast crash recovery (~1 min) independent of job_timeout. Completes the BA2d-1 DB-lease design as the SOLE build-liveness authority.
- [ ] BA2e ingest/build triggers + job endpoints + SSE
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
