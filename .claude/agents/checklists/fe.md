# FE checklist — web/ changes

Loaded by code-reviewer routing when the diff touches `web/`. FE lessons live
primarily in the catalog (`.claude/memory/graphrag-lesson-classes.md`) — read
the classes below when their trigger matches; this file holds only the routing
and the FE-specific operational rules.

Catalog classes to read for FE diffs (each has 何時比對 + the full rule):
- **Class 17** — cache-trust predicate × decision-surface four axes (lock
  predicate `isPending || isFetching || isError`; every decision entry point;
  idem-key grain trilogy; retry-the-goal vs retry-the-input). ANY review/
  decision UI or cached render.
- **Class 19** — derived DAG of a multi-query page (which failure kills which
  cache; page-level vs local verdicts).
- **Class 20** — mutation host lives at a non-unmounting level; one
  useMutation observer tracks ONE mutation; sentinel state machines
  enumerated first.
- **Class 21** — mocks align to the REAL error envelope (read the server /
  integration tests first).
- **Class 22** — translation is an assertion (read the producer); panel
  titles/framings are SCOPE assertions; aggregate gauges ≠ actionable counts.
- **Class 23** — write-side bricking: client mirror pinned to the real server
  validator via a shared corpus; unsaved-state for missing/malformed blocks;
  untouched fields from a fresh read.
- **Class 18** — contract projection stack (codegen/typetest faces; see also
  checklists/contracts.md when contracts/ changed too).
- **Class 26** — probe discipline (world-state stubs, probe-of-probe,
  grep-count after restore).
- **Class 30** — the dossier must read down to the worker's consumption layer.

FE-specific operational rules:
- **UI-visible string renames sweep `web/e2e/*.spec.ts`** — Playwright e2e is
  OUTSIDE check-all/CI (#104; until H18 lands), so a stale spec string is
  invisible to every mechanical gate. Grep the old string across `web/`
  INCLUDING e2e specs.
- **UI-flow tasks require the Playwright e2e tier** (LOOP step 4) and tests
  that encode why; component tests mock at the API-client seam (`api.GET`),
  not `globalThis.fetch` (openapi-fetch binds fetch at createClient).
- **Reliability affordances the contract exposes are mandates**:
  Idempotency-Key on writes, `meta.build_id` pinning across pages (fail-loud
  on cross-page mismatch), opaque cursors passed back verbatim (#68).
- **Retained state gates on identity** (`job_id === jobId`) — carried-over
  state across an identity flip is stale until proven otherwise (#82).
- **vitest `globals: false` needs explicit RTL `cleanup()`**; npm `overrides`
  for peer conflicts (#65).
