---
name: graphrag-v2-frontend-scope
description: Owner opened v2 frontend (FE1-4) on 2026-07-12 after v1 complete; FE2 blocked on BA4/DR-002
metadata: 
  node_type: memory
  type: project
  originSessionId: 1ca14cc5-3e0a-461e-ae04-ca06338ee1f2
---

v1 milestone fully merged (Track 0 contracts, Track 1 core C1–C11 + C8b MCP-HTTP, Track 2 backend BA0–BA8, Track 3 v1 frontend FE0/FE7/FE8/FE5/FE6). On **2026-07-12** the owner (via AskUserQuestion at the v1-complete gate) chose **"Open v2 frontend (FE1–FE4)"** — so v2 FE work is now in scope.

Dependency readiness of the four v2 FE items (the loop takes the top unchecked, respecting deps):
- **FE1 Import** — likely buildable on existing endpoints (BA1b projects/sources CRUD + BA2e ingest/build triggers + jobs/SSE). Verify before building.
- **FE3 Inspect** — buildable on existing BA3a/b/c inspection endpoints (documents/chunks/entities/relations/graph subgraph).
- **FE2 Clean** — **BLOCKED**: needs BA4 (cleaning preview/rules), which is v2-deferred and the **frozen contract has NO cleaning endpoints** (see TASKS.md BA4 line). Building FE2 requires a **DR-002 contract round first** (freeze the API shape, bump schema_version, record in DESIGN §26) — that's an owner decision; **stop and ask before touching the frozen `contracts/`**.
- **FE4 Graph explorer** — partial: BA3c subgraph endpoint exists, but an interactive graph-explorer may need more contract/backend. Assess when reached.

Rule carried in: opening v2 FE does NOT authorize changing the frozen contract; any FE task that needs a new endpoint/schema is a **DR-002 gate → surface to owner**, don't unilaterally edit `contracts/`. Apply the FE dossier discipline (incl. the #68 reliability-affordance row + #69 id-domain/display row) per [[graphrag-loop-paused-pr5]].

Non-blocking follow-up still open (owner deferred deciding): merged FE8 `useCancelJob` lacks an Idempotency-Key (same write-retry class fixed in FE5) — worth a small task if console writes should be uniformly retry-safe.
