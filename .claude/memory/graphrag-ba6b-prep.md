---
name: graphrag-ba6b-prep
description: BA6b (graph/hybrid REST query endpoints) design-time audit — prepared during the PR
metadata: 
  node_type: memory
  type: project
  originSessionId: 1ca14cc5-3e0a-461e-ae04-ca06338ee1f2
---

# BA6b prep — graph/hybrid on the BA6a seam (audited 2026-07-10)

BA6a (PR #60) built `_load_policy`/`_run_mode` in `api/routers/query.py`; BA6b adds
`POST /projects/{p}/query/graph` and `/query/hybrid` on the SAME helpers, so it
**inherits for free**: short-lived policy connection (R2 P1), 404→409→400→503
precedence (R3), preflight-outage typed 503 (R4-A). Audit findings to bake in at
design time (the [[graphrag-loop-paused-pr5]] zero-round pattern):

## options — the contract channel for graph params
- Frozen `QueryRequest.options`: "Mode-specific options (e.g. graph: max_hops;
  capped by query_policy)". BA6a rejects `options` loudly because semantic/sql/
  global support none; BA6b must ACCEPT a **validated vocabulary** for graph/
  hybrid: `{template, entity, other_entity?, hops?}` → `GraphQueryParams`
  (core.query.graph). Unknown option keys → loud 400 (BA6a precedent inverted
  per-mode, never accept-and-ignore).
- Layer parity with MCP: **types** (str/int shape) = Pydantic sub-model → 400
  VALIDATION_ERROR (MCP's tool schema enforces the same layer); **vocabulary +
  hop ceiling** (unknown template, hops > max_graph_hops) = core's
  `_validate_params` → in-envelope 200 + GUARDRAIL_BLOCKED warning (rejected,
  not clamped). Do NOT invent a REST-only 400 for template errors — the two
  facades must answer identically (BA6a module docstring's "one machinery, two
  facades").

## graph endpoint
- Runner: `run_graph(deps.graph, deps.repo, policy.cypher_policy(), params,
  label, policy.max_graph_hops)`; label = `query or f"{template}({entity})"`
  (MCP precedent, `core/mcp/server.py` graph_query tool).
- `top_k`: the MCP graph tool exposes NONE — reject `top_k` on the graph mode
  loudly (400, "unsupported for this mode"), the R1 lesson (never
  accept-and-ignore) + the owner's reject-while-unsupported precedent.
- `deps.graph` comes free: `project_query_context` already puts the lazy Neo4j
  driver in `ProjectContext` (api/deps.py), and the shared seam binds
  `deps.graph` per call exactly as MCP does — no new plumbing.

## hybrid endpoint
- **remaining_ms is LOAD-BEARING** (class-11, the C8 face): the runner must call
  `hybrid_policy(policy, body.top_k, latency_budget_ms=remaining_ms)` — hybrid's
  internal pacer runs on what binding LEFT of the §21 budget, never a fresh full
  one (see the MCP hybrid tool's comment). BA6a runners ignore `_remaining_ms`;
  hybrid is the first REST runner that must consume it. Discriminating pin:
  fake seam passes a sentinel remaining_ms → assert hybrid_policy received it
  as latency_budget_ms.
- Graph params inside hybrid: MCP builds params only when template AND entity
  are both present, else graph mode is skipped in-envelope with a reason.
  REST: options are validated (above), so partial graph options → loud 400 at
  the shape layer; complete-and-absent → skip-with-reason parity.
- `top_k` threads via `hybrid_policy` (already clamps).

## cap-chain lesson (R4-B, generalize)
Any cap that maps to `QueryRequest.top_k` must clamp through `policy.top_k()`
(the frozen `max_top_k` upper bound) BEFORE meeting a mode-level row ceiling —
sql was the offender in BA6a (`[1, 20, 100]` pin). Check every new cap in
BA6b against the frozen `contracts/query_policy.schema.json` field
descriptions FIRST (the reviewer's recorded self-correction from #60 R4).

## tests
- Component: per-endpoint reprojection; options vocabulary reject (unknown key
  400); graph guardrail parity (unknown template → **200** + GUARDRAIL_BLOCKED
  warning, NOT 400 — discriminates REST divergence); graph top_k reject 400;
  hybrid remaining_ms sentinel pin; held_connections==0 pin extends free.
- Integration: graph e2e needs Neo4j (mark integration); hybrid e2e needs the
  LLM selector — fake at api.deps like BA6a's global e2e.
