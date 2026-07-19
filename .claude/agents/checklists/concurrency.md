# Concurrency checklist — TOCTOU, multi-commit orchestration, state machines, clocks

Loaded by code-reviewer routing when the diff touches locks, multi-transaction
control flow, concurrent admin surfaces, or ordering-bearing timestamps.
Catalog cross-refs: class 10 (TOCTOU), class 26 (探針紀律) in
`.claude/memory/graphrag-lesson-classes.md`.

- **Bind-time check ≠ invariant (TOCTOU)**: a precondition validated at
  construction/binding does not stay true for later operations. Fold the
  recheck into the mutating statement (atomic) or hold the right lock — and
  prove the concurrent interleaving on live infra, not by reasoning (C1b: a
  writer validated as `building` at bind time kept writing after activation;
  fix = `INSERT..SELECT..WHERE EXISTS(status='building' FOR SHARE)`. A plain
  recheck still races an uncommitted change — MVCC readers don't block
  writers.)
- **Lock/TOCTOU tests probe DURING the protected window, not after**: a later
  incidental lock masks the bug and the test passes with the fix removed
  (BA2a: pause inside the window — monkeypatch to block after the lock,
  before the mutation — then probe; always revert-probe a lock test). A race
  test can ALSO be false-green by never REACHING the guard: if the concurrent
  writer's own precheck short-circuits first, the guarded statement never
  runs (BA2c-1: the racing cancel read `done` and skipped the UPDATE). Force
  the exact interleaving deterministically (feed the writer a stale snapshot)
  — timing-based "create_task then release" guarantees nothing.
- **A multi-commit orchestrator is a TOCTOU/crash-window factory**: control
  flow spreading one logical operation across N transactions has a
  crash-or-race window at EVERY commit boundary — walk each boundary asking
  "a crash here, or a concurrent op here — what breaks?" up front (BA2c-1: 4
  of 7 findings were exactly this — create+attach split → orphan build;
  terminal+finalize split → job stuck running; cross-connection cancel read →
  accepted-but-ignored limbo). Fixes: fold related transitions into ONE
  transaction; decide under the row lock that serializes the racing writer;
  record-in-txn so related rows commit atomically. A cross-connection signal
  read (read a flag on conn A, act on conn B) is inherently racy — the
  decisive read goes under the same lock as the action, and the racing
  writer's UPDATE is status-guarded (`WHERE status IN active`) so a lost race
  no-ops.
- **A state-machine surface gets a MATRIX sweep, not point fixes**: for a
  lifecycle with concurrent operations (activate/rollback/prune; job queues;
  review flows), enumerate at design time: every OPERATION × every STATUS
  (each cell a deliberate act/skip/refuse) × every PAIR of concurrent
  operations (what serializes them). Selection queries are part of the
  matrix: a target SELECTED outside the serialization that promotes it can go
  stale (C9: five rounds opened cells one at a time).
- **Ordering-bearing timestamps use ONE clock**: never mix the application
  clock and the DB clock in timestamps feeding an ORDER BY that decides
  behavior (C9: container clock skew reordered rollback history). Pick the DB
  clock for DB-ordered history. Clock STABILITY GRADE is part of the
  contract: PG `now()` is transaction-stable — per-decision sequencing needs
  `clock_timestamp()` (#59; catalog class 4).
- **Determinism claims need a discriminating fixture + a revert-probe**: any
  value anchoring idempotency/reproducibility (dedup identity, partition,
  capped LLM sample) must be a pure function of its input SET — never of
  Postgres fetch order. The test must be proven able to fail: revert-probe
  BEFORE submitting, with a fixture that can discriminate (C7: a symmetric
  fixture was permutation-invariant — 720/720 orders one answer — so two
  successive "determinism" tests were false-green). Full probe discipline:
  catalog class 26.
