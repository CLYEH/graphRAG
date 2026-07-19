# Contracts checklist — frozen-schema changes and emission surfaces

Loaded by code-reviewer routing when the diff adds/changes a `contracts/` schema
or emits stored values through a frozen schema. Catalog cross-refs: classes 23
(散文承諾), 17→18 (投影疊層), 1 (驗值) in `.claude/memory/graphrag-lesson-classes.md`.

A contract object exists in FIVE projections, and "the contract says X" must
hold — and be pinned — in EACH one independently (class 18; BA4 burned 5 rounds,
one per face of a single request object). Fixing the face a finding names leaves
the next face as next round.

- **Schema text** (class-1 value constraints: no-op values, cross-field pairs,
  additionalProperties). AND the guarantee→structural-pin matrix (class 24;
  UXC1a, 5 rounds — one per unpinned promise): every "never/always/must/only"
  the description or DESIGN record PROMISES must have a STRUCTURAL pin in the
  schema, or a conformant server can violate it. Run the matrix before first
  push: "no silent drop" → `minItems: 1`; "rejected item states its reason" →
  the rejected variant `required: [reason]` + `minLength: 1`;
  mutually-exclusive variants → `oneOf` with the OTHER variant's keys declared
  as the `false` schema (`document_uri: false`) so codegen emits `?: never`.
  Fixed-semantics request bodies get `additionalProperties: false` — enumerate
  ALL new bodies at once (#94). A defined-but-UNREFERENCED component is DEAD —
  it pins nothing: grep every new schema for a `$ref` and either wire it into
  a request/response or delete it. A correlation key the design relies on to
  map a response row back to its request is `required` + non-null in EVERY
  variant. A guarantee a static type CANNOT capture (per-project exposure
  allowlist) is not dropped: freeze the POLICY SHAPE and document runtime
  enforcement + its test (query_policy precedent) — without minting another
  unreferenced component. Advertised states must be producible by the
  endpoint's own routing shape — no vacuous promises (#94). Align the prose to
  the schema, not the reverse (class 3 self-consistency on the contract face).
- **Runtime validator equivalence**: Pydantic's LAX defaults diverge from JSON
  Schema in at least three places — bool passes `int` (coerces; `strict=True`),
  numeric strings pass `int` (`strict=True`), and explicit `null` passes
  `X | None` as if OMITTED while `required`-but-non-null schemas reject a
  present null (mode="before" null guard). Booleans that GATE state changes
  must be `strict=True` — a `"false"` string 400s, never silently disables
  (#95). Write the acceptance-matrix test: {absent, explicit-null, wrong-type
  (bool AND numeric string), unknown-key, each combinator branch} — each case
  DISCRIMINATING (BA4: a coerced value 400'd by ACCIDENT; pair every case with
  values that keep the coerced result otherwise legal).
- **Generated client type**: bare `required`/`not` combinators codegen to
  `unknown | unknown` — variants must be CONCRETE closed schemas; a structural
  union is not an EXACT one (excess-property checks cover literals only) —
  declare the opposite variant's keys with the `false` schema (OpenAPI 3.1).
- **Compiler flags that make the type real**: without `strict`
  (strictNullChecks), every null-guarantee in the generated type is hollow.
  Pin with a `*.typetest.ts` of `@ts-expect-error` assertions — which doubles
  as the tripwire (removing strict turns the pins into TS2578 failures).
- **Probe the probe**: a mutation-based revert-probe proves nothing unless the
  mutation is ASSERTED to have landed (BA4: a strict-removal replace silently
  no-opped and reported a false-negative 0). Full probe discipline: catalog
  class 26.
- **A stored value served verbatim through a frozen schema is an emission
  surface**: audit every WRITER of that value against the schema's own
  required set — read the schema, never a comment's paraphrase of it (BA2e-1:
  a column comment omitted `request_id`, so both writers stored a
  contract-invalid object served straight through). The audit covers EVERY
  field's typing: a nullable column behind an optional NON-nullable contract
  property must OMIT the key when NULL — emitting `null` is schema-invalid
  (BA3a). The nullability matrix runs on BOTH sides — request parsing
  (omitted ≠ null positions) AND response emission — × every field.
- `contracts/` is FROZEN (DR-002): any change requires a `schema_version` bump
  + DESIGN §26 record in the same task, and the owner's sanction.
