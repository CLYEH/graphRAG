# Guards checklist — deny surfaces, validators, parsers, untrusted input, projection reads

Loaded by code-reviewer routing when the diff adds a security/scope guard, a
capability boundary, validates a precondition, parses untrusted input, or reads
from a derived projection. Catalog cross-refs: classes 9 (防護面), 1 (驗值), 5
(檢查者/消費者), 14 (無界表面), 16 (雙 gate 同集), 27 (可達集) in
`.claude/memory/graphrag-lesson-classes.md`.

- **Reject-surface completeness** (class 9): a guard that bans a dangerous
  construct must enumerate EVERY sibling API that produces the same effect, at
  every nesting depth — not just the one the reviewer named (C1b: raw SQL
  escaped the build scope via `text()` → `literal_column()` → nested
  `or_`/`and_` → `op()`/`bool_op()` strings — one guard, three rounds). Seeing
  one banned constructor, grep the library surface for its siblings and
  compose/nest them in a test.
- **No over-block (the dual)**: tightening a guard must not reject legitimate
  inputs — a blanket ban is as wrong as a leaky one, and needs a POSITIVE
  acceptance test beside the attack tests (C1b: a blanket `custom_op` ban
  killed the JSONB `->>`/`@>`/`?` operators). Attack-only tests are
  false-green against over-block. Gate on the value/string, not the type.
- **Query-language guard = grammar sweep** (class 14 boundary): when the
  guarded surface is a query LANGUAGE, the dangerous-construct siblings are
  GRAMMAR CLAUSES — walk the clause list before first push: FROM modifiers
  (TABLESAMPLE, alias column-lists, schema qualification), SELECT modifiers
  (DISTINCT, INTO, FOR UPDATE/SHARE), row selection (OFFSET; LIMIT expression
  FORMS — `is_int`, not the "not a string" proxy that admits `LIMIT 2.5`),
  ordering (positional ordinals incl. parenthesized `ORDER BY (1)` —
  `.unnest()` before the check), set-ops/CTE/subquery/join, function calls,
  placeholders — each × its over-block dual. Prefer STRUCTURAL ELIMINATION
  over a guard when the design allows (parameterized Cypher templates: 0
  rounds; the NL→SQL guard faced the whole grammar: 19). Two adjacent rules:
  a store's REPRESENTATION LIMITS are value domains (>63-byte identifiers
  silently truncate — reject, don't corrupt), and a type check AFTER a
  coercing extraction sees only the coerced value — gate at the layer where
  the coercion happens (`jsonb_typeof(...)='string'` in SQL, not `isinstance`
  on the driver's stringified text).
- **Untrusted-input value tree (sweep depth-first, once)** (class 1): when
  consuming structured output from an untrusted source (LLM, external API),
  walk the ENTIRE value tree before first push — envelope, array items, leaf
  scalars — and at EVERY leaf apply {absent, wrong type, empty, BLANK
  (whitespace), out-of-vocabulary, unlocatable reference} (C3b burned 5
  rounds one level per round). Hard rules: shape validation lives INSIDE the
  failure boundary (wrong shape = failed item, not crashed pass); no `str()`
  coercion of identity/evidence-bearing values; any join/dedup key derived
  from untrusted input uses the STORE'S own identity function — checker and
  consumer, one identity (class 5).
- **A config/parser loader's completeness face is input-position × level**:
  enumerate {absent, explicit-null, unknown-key, wrong-type (bool-is-int),
  out-of-vocabulary} × {top level, each nested block, each rule, each leaf} —
  every cell fails loud OR is documented. Omitted ≠ explicit-null must not
  collapse (`raw.get(k)` returns None for BOTH — branch on `k not in raw`).
  Delegate business rules to the typed target's own validator (single
  source). Leniency (ignoring unknown keys) is licensed ONLY at a genuine
  free-form CONTRACT boundary; every CLOSED nested schema REJECTS unknown
  keys, else a typo on an optional key silently disables it. Boundary test:
  "are this level's keys DATA or SCHEMA" (BA2c-2a). **The WHOLE BODY is
  itself a position**: `Model | None` binds explicit JSON `null` to None,
  indistinguishable from absent — {absent, empty-object, JSON-null,
  wrong-top-type} belong in the matrix (BA2e-1 round 5).
- **Per-surface inventory (transfer ≠ sweep)**: a boundary module's NEW
  surfaces get their own catalog pass — family-pattern transfer is not a
  per-surface sweep (C1c 1:1 = 0 rounds; C1d new surfaces = 4). THE MODULE'S
  OWN API ARGUMENT SHAPES ARE SURFACES TOO (C3a). Inventory explicitly:
  identifiers COMPOSED from contract strings (validate EVERY member of a
  composite — half a citation is uncitable; encode joins LOSSLESSLY —
  length-prefix, else ("a:b","c") collides with ("a","b:c") and dedup
  SILENTLY DROPS data); accepted vocabularies and (selector × gated-field)
  pairs, typos rejected on read AND write paths — including two fields that
  assert the same fact (validate agreement at the door); ids typed as what
  they are (a row id is a UUID, not str); every side effect beyond data
  writes.
- **State × verdict matrix (total, re-run after every fix)**: when the diff
  introduces or consumes a state vocabulary, build the full cross-product
  table — EVERY cell a deliberate outcome — and REBUILD it after each fix,
  because fixes add states/transitions (C4: round 4 introduced
  `needs_review`, round 5 found approve had no exit for it; a noted lesson is
  not a prevention, the table is). Composed identity keys are part of this: a
  key that embeds a disambiguator ASSERTS distinctness — consumers must honor
  the assertion, not merely use the key for equality (class 28).
- **Handoff completeness**: when a step returns a subset for a downstream
  consumer, trace the need through EVERY branch — especially the skip/no-op
  branch that "did nothing" this run but must still forward its item (C2:
  crash-retry re-ran ingest, skipped the committed doc, stranded the build
  unchunked). "Consumer can handle what it gets" ≠ "consumer gets everything".
- **Name the threat model**: for any derived value guarding an invariant
  (hash suffixes, dedup keys, fingerprints), state ACCIDENTS vs ADVERSARIES
  before judging strength — contract-valid input is attacker-influenceable
  (C1d: a 40-bit hash suffix fell to brute force in seconds).
- **Preflight the consumer's property, not a proxy** (class 5): a precheck
  verifies the thing the consumer depends on, not a cheaper stand-in (C1d:
  `ensure_collection` checked existence, not vector size/distance).
- **The trust boundary is per-COLUMN, not per-store**: an UNCONSTRAINED
  reference column (bare uuid[]/text ref, no FK) in the SoR is as untrusted
  as a projection value — ground such refs against the table they claim
  before emitting (C6d). Corollaries: the FIXTURE must satisfy the property
  the check verifies; protocol limits are value domains (32767-bind cap →
  batch unbounded IN lists).
- **Partial validity of an untrusted selection is WHOLE-answer failure**:
  a mixed answer (valid + out-of-vocabulary members) must not be
  half-trusted — intersect-and-continue silently narrows; one bad member
  distrusts the whole answer → the documented breadth fallback applies (C6e).
  Check the documented failure rule WORD BY WORD against the implementation.
- **Retrieval read/emit surface (untrusted projection → SoR re-verify →
  frozen contract)**: a derived projection (Qdrant/Neo4j) is UNTRUSTED — the
  read-side dual of the value tree. Every payload value reaching output is
  validated-against-the-SoR-or-DROPPED: never raises, never emits
  unvalidated. Ids from the validated key (corrupt → drop the hit); optional
  display fields coerce to null (hit stays); every drop counts into
  `PARTIAL_RESULTS` (§22 degrade-not-fail). Sweep SYMMETRICALLY: every
  result_type × every SoR invariant (existence AND `status='active'` —
  forward-only projection retains rows the SoR later excluded). GRAPH reads
  re-verify the whole TRAVERSAL: {corrupt values, node status, EDGE
  existence, REACHABILITY (recompute over SoR-active elements), SHORTEST-ness
  (exclude stale elements and retry)}. Verification REJECTS THE CANDIDATE,
  NOT THE SEARCH (run inside the candidate loop, search continues). Resource
  ceilings are guards too, symmetric across every result source (a cap on
  entities but not appended edges bypasses §21; judge truncation at FETCH
  time). An ABSENT derived-store artifact is a legitimate producer state
  (Qdrant 404 → `[]`/`0`, not 500) — but a non-404 store error still
  surfaces (don't launder an outage into an empty answer). (C6a/C6c.)
- **A new domain error's completeness face is the function's callers, not
  your diff**: adding an exception to a SHARED function pulls in EVERY
  caller's translation — trace to each consumer's error mapping or it falls
  through to 500 (BA2a). "This slice has no HTTP" is wrong if the touched
  function is HTTP-consumed.
- **An error precedence is part of the CONCEPT, not the surface**: when a new
  entry surface serves a concept whose siblings already fixed an ordering
  (404 → NO_ACTIVE_BUILD 409 → validation gates), the ordering transfers at
  DESIGN time. The durable fix is structural — one shared helper owns the
  precedence; reviewing a new surface, diff its gate order against the oldest
  sibling's (#57→BA6a: the same bug re-derived verbatim one family later).
- **A new entry point to a fenced surface must carry the fence**: a
  convenience path (factory overload, bypass constructor, admin hook) to a
  construction-fenced surface demotes the guarantee from structure to caller
  discipline unless it demands equally strong proof (C8: `bound_to(raw uuid)`
  bound archived builds until the ActiveBinding capability proof; mind
  `dataclasses.replace` forgeries — tokens live in InitVar).
