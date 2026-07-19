# Lifecycle checklist — frameworks, DI, async handlers, request invariants

Loaded by code-reviewer routing when the diff touches worker/job handlers,
framework dispatch/retry/timeout/streaming, DI providers, request-scoped
invariants, or judge/metric surfaces. Catalog cross-refs: classes 12 (框架生命
週期), 13 (eager DI), 25 (寫入授權), 11 (請求級不變量), 26 (隱含預設/探針) in
`.claude/memory/graphrag-lesson-classes.md`.

- **Framework lifecycle mechanism × application-owned SoR/liveness (class
  12)**: when a handler runs on a FRAMEWORK's execution lifecycle, read the
  framework's EXACT semantics from source and write the "framework exit list"
  BEFORE implementing — its timeout can cancel your handler before the SoR
  row terminalizes and NOT retry (arq `wait_for` outside the retry branch);
  its keys outlive your intent (in-progress key blocks re-dispatch; a kept
  result reserves a custom id for keep_result seconds). Defenses (each cost a
  round in BA2d): (a) never couple crash recovery to the framework timeout —
  own the liveness (DB lease + reaper), framework timeout = generous
  backstop; (b) the liveness marker brackets the ENTIRE handler (acquire as
  the first statement); (c) a recovery/re-dispatch channel is ITSELF a
  lifecycle: who re-dispatches, how it races the live original, deterministic
  per-generation dedup id (stale-lease expiry as generation marker), can any
  framework key reserve that id across a failed generation, and the scan's
  cost (index it); (d) recovery racing a slow-but-alive original degrades to
  a benign no-op via the SoR's own atomic status check; (e) ENABLING a new
  framework mode/transport re-opens this audit for EXISTING code whose
  invariants were mode-dependent (C8b: a module-level runtime slot sound
  under stdio was corrupted by HTTP session multiplexing — use the
  framework's per-session channel); (f) a yield-dep's lifetime COMPOSES with
  pool capacity: FastAPI yield-deps live until the RESPONSE completes
  (streaming holds the txn for the stream's life), so a held dep + a second
  acquire from the SAME pool convoys at capacity (BA6a) — prechecks take a
  short-lived acquire-use-release connection.
- **Config snapshots pin at JOB CREATION** (scalar subquery in the INSERT),
  not first dispatch — the queue-delay window otherwise reads config the
  operator changed after submit (#51).
- **Best-effort background tasks** (heartbeat, cleanup): exceptions surfacing
  through `finally`'s await MASK the main result — contain everything except
  cancellation (#50).
- **Eager dependency acquisition (class 13)**: framework DI resolves before
  any handler logic, so any work at resolution time (opening a pool,
  config-validating construction) makes paths that never need the resource
  fail with it (Redis down broke idempotent replay; invalid Neo4j config
  broke a 404). Providers are zero-I/O AT RESOLUTION; acquire inside the
  exact branch that needs the resource; pin discriminatingly
  (broken-resource + non-dependent path → the domain answer, not 500) and
  structurally (raising providers on branches that measure nothing).
- **Write-authorization matrix for async job handlers (class 25)**: a handler
  on a lease has ONE write-license predicate ("under the row lock: status
  still active ∧ lease still mine") — derive it once, ENUMERATE EVERY DB
  write site (mark-running, each failure terminalization, finalize, result
  persist) and apply it at each; a lapsed lease can hand the job to a
  replacement DURING any phase (#81 added these one write-site per round).
- **A fix that adds machinery is itself a new surface**: a guard/pin/lease
  check introduced DURING review must be re-swept against this checklist
  before push — especially "the check and the consumption share one
  read/lock" (#81: the fingerprint recheck read files once, the parser
  re-read them — an edit between the reads scored as verified). If later
  rounds are reviewing your fixes rather than the task, the fixes skipped
  their catalog pass. Reading retained/carried-over state across an identity
  flip must be gated on that identity (#82).
- **An implicit default does not survive time or a handoff (class 26)**: a
  DERIVED default (newest-X, first-eligible) is valid only at derivation. Any
  operation that OUTLIVES it (async job whose terminal refetch re-derives) or
  CROSSES pages (a CTA link with a different default rule) must MATERIALIZE
  the value: pin as explicit state at start, carry it in the link
  (`?build=<id>`). Pair every "default + thing that acts" and ask: "will this
  re-derive to the same value when the action lands?"
- **Invalidation/refetch probes need WORLD-STATE stubs, not call counts
  (class 26)**: a call-count stub hands a buggy extra refetch the "after"
  world and the assertion passes with the fix reverted. Model world state
  (flags flipped when the modeled event happens, behind a released promise);
  prove discrimination by running the mutation (assert it landed), watching
  the exact test go red, restoring from a TEMP COPY — never `git checkout --`
  over uncommitted work.
- **A request-scoped invariant is swept over the WHOLE lifecycle, once
  (class 11)**: enumerate every phase — binding/acquisition, selection, each
  mode, discovery, assembly — against the invariant IN ONE PASS. "Every phase
  has a cap" does not give "the request has a cap" (C8: the §21 deadline took
  five rounds — thread REMAINING budget, don't restart it). The lifecycle
  INCLUDES segments BEFORE the enforcing seam: a typed-degradation guarantee
  enforced inside the bounded seam doesn't cover a preflight read that runs
  first — the preflight maps it itself (BA6a R4).
- **The idempotency replay decision precedes ANY scope-row precheck**: a
  stored response must replay — and a different-hash reuse must 409 — even
  after the scoped row legally vanished (terminal job CASCADE-deleted). An
  existence-404 before the replay lookup breaks §27 exactly when the client
  needs it (BA2e-1). Peek replay/conflict first (non-reserving); precheck
  only fresh requests.
- **A judge/scoring surface gets its SEMANTICS SPEC first**: when code SCORES
  or GATES other code (eval harnesses, ranking, acceptance), write the
  complete matching semantics BEFORE implementing — identity model, which
  stores must AGREE, degradation (§22 — a judge never crashes into
  "unmeasured"), comparability (suite + policy + model identity). Reactive
  cell patching is the anti-pattern (C10: 17 rounds).
- **A "queue"/"count" metric's denominator is the whole spec state machine**:
  enumerate EVERY spec state that qualifies before implementing — each
  missing state is a false-dark light hiding real work (C11: pending_review
  took two rounds; list states from the spec, not memory).
- **Config a caller must wire by hand is dead config**: a settings field that
  defaults to a literal instead of reading `get_settings()` silently ignores
  the operator. A new tunable is READ on the default path, with an explicit
  override, or the knob is decorative.
- **A guarantee on a framework boundary must enumerate EVERY exit**: when
  code claims an invariant on a framework's error/response path, list the
  bypasses — polymorphic handler precedence, hidden serialization failures
  (non-JSON in an error body crashes into the 500 path), framework
  side-channels (405 Allow / WWW-Authenticate / Retry-After headers),
  consistency between your mapping and the deeper contract. Centralize the
  fix (one encoder over the whole body), don't patch handlers one by one
  (BA0: 4 rounds).
