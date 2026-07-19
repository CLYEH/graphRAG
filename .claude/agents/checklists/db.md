# DB checklist — schema, DDL, migrations

Loaded by code-reviewer routing when the diff touches `core/stores/tables.py` or
`migrations/`. Run BEFORE the first push, cell by cell; FAIL on any unjustified gap.
Catalog cross-refs: class 1 (契約/DDL 驗值), class 8 (邊界語意/表示誤差) in
`.claude/memory/graphrag-lesson-classes.md`.

- **Per column**: NOT NULL matches the frozen contract's required lists; enum
  CHECKs match frozen vocabularies (lockstep-tested both ways); identifiers,
  hash inputs, and contract-`minLength` fields ban `''`; contract minimums
  become range CHECKs; definitional sanity holds (end ≥ start, positions ≥ 0).
  A **nullable JSON/JSONB column that stores Python `None` to mean "absent"
  needs `none_as_null=True`** (or insert `sa.null()`) — otherwise `None` binds
  as the JSONB `'null'` LITERAL, not SQL NULL, so `col IS NULL` is false and
  any two-state CHECK / replay keying on absence misfires (BA1b; class 8). A
  DTO whose field is nullable must match the column's nullability: an explicit
  `null` on a NOT NULL column is a 400 at the boundary, never a NOT NULL
  IntegrityError surfaced as 500.
- **Per table**: every frozen identity key gets a UNIQUE (entity_key,
  relation_signature once minted → partial, merge_key → LEAST/GREATEST
  expression index, position keys like (document_id, ordinal), dedup hashes).
- **Cross-table**: child FKs are composite over scope columns (build_id, and
  project where both sides have it — DR-006 makes mixing unrepresentable);
  parents expose matching UNIQUE FK targets; EVERY child FK has a supporting
  index (Postgres doesn't auto-index FKs).
- **Migration (constraint-tightening)**: a migration that ADDs a constraint
  (FK / CHECK / NOT NULL / UNIQUE) to an EXISTING table must reconcile
  pre-existing violating rows FIRST — backfill or clean them in the same
  migration, BEFORE the `ALTER`. Rows accumulate while the constraint doesn't
  exist (especially in the window since a related earlier migration). CI
  migrates a **fresh empty DB**, so this failure is invisible to the check
  (class 5: the check's DB state ≠ a real dev/prod DB) — reason about the
  populated case explicitly, and test it by applying the migration over a row
  that would violate it (BA2b: downgrade → insert an orphan → assert upgrade
  succeeds).
- **Conditional**: every (type, per-type required fields) pair is CHECKed both
  directions (must-have AND must-not-have) — and write the IFF as an EXPLICIT
  two-branch disjunction: `(cond) = (A AND B)` under-enforces the false branch
  (one true conjunct satisfies NOT(A AND B) — C3c). Expand the corners (2^n)
  and prove each rejected on live PG; a "both directions" test that only
  drives the corners the weak form happens to reject is false-green.
- **Emission path**: for each result_type, walk the frozen source_ref
  requirements back to stored columns — every required field must be derivable
  from NOT NULL data (denormalized where prune survival demands).
- **Universal-sounding tests** ("every child FK has an index") must enumerate
  ALL instances from information_schema/catalog or they are false-green (#17;
  FAIL). A deliberate gap needs a stated, distinguishing rationale that
  survives the exception-side rewrite test.
- **A new parent table over existing un-FK'd data owes a three-face
  completeness check** (BA1a→BA2b): (1) DELETE — every child/sibling table
  keyed by the bare string that a parent-row delete would orphan or leave to
  carry forward onto a recreated same-name parent; (2) UPGRADE — the migration
  backfills the parent from the pre-existing keyed tables, and when the FK
  lands in a LATER migration, that migration RE-RUNS the backfill before
  `ADD CONSTRAINT` (the in-between window is unconstrained); (3) WRITE-RACE —
  a count-then-act guard is a TOCTOU until a real FK (or shared lock) binds
  parent and child; name the FK and who must take it.
- **A fix that changes a stored shape owes its own lifecycle sweep, at fix
  time** (BA2e-1 rounds 1→4 were one causal chain): (1) rows ALREADY WRITTEN
  under the old shape — reconcile-before-constrain migration + a populated-DB
  upgrade test (the Migration bullet applies to mid-PR FIXES too); (2) a
  recurring QUERY the fix adds gets its support structures — a partial index
  mirroring the predicate, with parity against the sibling mechanism's; (3) a
  CAP/limit chain the fix introduces is validated against the frozen schema's
  FIELD DESCRIPTIONS, not just its structure (BA6a: `min(top_k, sql_rows)`
  contradicted `max_top_k`'s description) — a cap is correct only against the
  contract's words for every field it touches.
