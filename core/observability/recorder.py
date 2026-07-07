"""Pipeline run recording (§18/§27.7; C11) — the three-layer write path.

SEMANTICS (spec-first — the judge-surface lesson):

- **One run = one record_run call.** The run row binds §27.7's build rule at
  the database CHECK (`pipeline_runs_build_binding`): every kind carries the
  building build's id; ONLY ``SOURCE_VALIDATION_RUN_KIND`` may pass
  ``build_id=None``. This module adds no second gate — the CHECK is the
  single enforcement point; a violation raises loud at insert.
- **Steps** persist their counters (input/output/skipped/failed) verbatim
  from the outcomes handed in: ``failed_count`` counts ``failed`` outcomes,
  ``skipped_count`` counts ``skipped``, ``output_count`` the rest. A step
  with any failed item is recorded ``status='failed'``, else ``'done'``
  — the run is ``'failed'`` if any step failed, else ``'done'`` (the frozen
  §27.2 JobStatus vocabulary: queued/running/done/failed/cancelled — the
  pipeline_runs_status_valid CHECK rejects anything else; §18's Console
  line reads these).
- **Item verbosity** (🔧 ``observability.item_logging``) decides which item
  ROWS persist; counters above are ALWAYS complete regardless:
  ``failures`` (default) → rows for failed+skipped only (the §18 frozen
  minimum — the §27.7 retry boundary reads exactly these);
  ``sampled`` → failures + every 10th success (deterministic by order —
  enough to eyeball throughput without the full volume);
  ``all`` → every item.
  An unknown verbosity value falls back to ``failures`` — the safe minimum
  is never silently widened, and the §27.7 retry input can never be lost by
  a typo'd config (fail-closed on the smaller set).
- **item_ref stability** is the producer's contract
  (:data:`core.observability.spec.ITEM_REF_KEYS`); the recorder persists
  what it is handed and never invents identifiers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.observability.spec import ItemOutcome
from core.stores import tables

#: the 🔧 verbosity vocabulary; anything else falls back to "failures".
ITEM_LOGGING_MODES = ("failures", "sampled", "all")

_SAMPLE_EVERY = 10  # sampled mode: failures + every Nth success


@dataclass(frozen=True)
class StepReport:
    """One pipeline step's outcomes, as the producer measured them."""

    step_name: str
    outcomes: tuple[ItemOutcome, ...]
    input_count: int | None = None


def _persistable(outcomes: tuple[ItemOutcome, ...], verbosity: str) -> list[ItemOutcome]:
    """Filter by verbosity, then DEDUPE by (item_kind, item_ref) at the
    first-seen position — FAILED dominates the kept status — §18/§27.7's own
    dedup rule, and the table's ``pipeline_step_items_dedup`` unique index
    makes a duplicate row roll the WHOLE run record back (reachable under
    default verbosity: ingest emits one skipped outcome per duplicate
    payload). Failed-dominates protects the §27.7 retry boundary, which
    replays the persisted FAILED rows across runs."""
    filtered: list[ItemOutcome]
    if verbosity == "all":
        filtered = list(outcomes)
    else:
        filtered = []
        success_seen = 0
        for outcome in outcomes:
            if outcome.status in ("failed", "skipped"):
                filtered.append(outcome)
            elif verbosity == "sampled":
                success_seen += 1
                if success_seen % _SAMPLE_EVERY == 1:  # 1st, 11th, 21st …
                    filtered.append(outcome)
    # dedupe by (item_kind, item_ref) — the unique index would roll the run
    # back otherwise — but FAILED dominates: the §27.7 retry-failed-only
    # boundary replays the persisted FAILED rows across runs, so a failed
    # occurrence must never be masked by an earlier skipped/success one for
    # the same ref (Codex round 7). First-seen position is preserved.
    index_of: dict[tuple[str, str], int] = {}
    deduped: list[ItemOutcome] = []
    for outcome in filtered:
        key = (outcome.item_kind, outcome.item_ref)
        if key not in index_of:
            index_of[key] = len(deduped)
            deduped.append(outcome)
        elif outcome.status == "failed" and deduped[index_of[key]].status != "failed":
            deduped[index_of[key]] = outcome  # failed dominates the kept row
    return deduped


async def record_run(
    conn: AsyncConnection,
    project: str,
    build_id: uuid.UUID | None,
    kind: str,
    steps: list[StepReport],
    *,
    verbosity: str | None = None,
    created_by: str = "pipeline",
    error: str | None = None,
    cancelled: bool = False,
) -> uuid.UUID:
    """Persist one run with its steps and (verbosity-filtered) items.

    ``verbosity=None`` (the default) reads 🔧
    ``observability.item_logging`` from settings — the tunable works without
    every caller wiring it; an explicit argument overrides.

    ``cancelled=True`` forces the run status to ``'cancelled'`` (the §27.2
    JobStatus vocabulary the CHECK already permits), overriding the
    failed/done inference — a cooperatively-cancelled build (BA2c) stopped
    between steps is neither a failure nor a clean completion; the steps that
    DID run are still recorded truthfully.

    LOANED-CLEAN connection (the C6b idiom): the caller must hand a
    connection with NO open transaction — rolling one back here would
    silently destroy the caller's uncommitted pipeline writes, and
    committing it would publish work the caller may still abort. This
    module opens exactly one transaction for the whole record (a
    half-written run would misreport §18's Console line)."""
    if conn.in_transaction():
        raise RuntimeError(
            "record_run requires a connection with no open transaction — commit or "
            "roll back the pipeline's own work first (a rollback here would destroy it)"
        )
    if verbosity is None:
        from core.config import get_settings

        verbosity = get_settings().observability_item_logging
    if verbosity not in ITEM_LOGGING_MODES:
        verbosity = "failures"  # fail-closed to the frozen minimum
    now = datetime.now(tz=UTC)
    run_failed = error is not None or any(
        any(o.status == "failed" for o in step.outcomes) for step in steps
    )
    run_status = "cancelled" if cancelled else ("failed" if run_failed else "done")
    async with conn.begin():
        if build_id is not None:
            # pipeline_runs has NO FK — an unverified (project, build_id)
            # pair would attribute observability rows to one project under
            # another's (or a pruned) build. Verify the binding INSIDE the
            # write txn, FOR SHARE, so a concurrent prune cannot delete the
            # build row between check and insert (the class-10 lesson:
            # bind-time checks alone are TOCTOU). Status is deliberately
            # unconstrained: recording a FAILED build's run is §18's whole
            # point, and the record may land right after the flip.
            owner = (
                await conn.execute(
                    sa.select(tables.builds.c.project)
                    .where(tables.builds.c.id == build_id)
                    .with_for_update(read=True)
                )
            ).scalar_one_or_none()
            if owner is None:
                raise LookupError(f"build {build_id} does not exist — cannot record a run")
            if owner != project:
                raise LookupError(
                    f"build {build_id} belongs to project {owner!r}, not {project!r} — "
                    "refusing a misattributed run record"
                )
        run_id: uuid.UUID = (
            await conn.execute(
                tables.pipeline_runs.insert()
                .values(
                    project=project,
                    build_id=build_id,
                    kind=kind,
                    status=run_status,
                    created_by=created_by,
                    started_at=now,
                    finished_at=now,
                    error=error,
                )
                .returning(tables.pipeline_runs.c.id)
            )
        ).scalar_one()
        for step in steps:
            failed = sum(1 for o in step.outcomes if o.status == "failed")
            skipped = sum(1 for o in step.outcomes if o.status == "skipped")
            output = len(step.outcomes) - failed - skipped
            step_id: uuid.UUID = (
                await conn.execute(
                    tables.pipeline_steps.insert()
                    .values(
                        run_id=run_id,
                        step_name=step.step_name,
                        status="failed" if failed else "done",
                        started_at=now,
                        finished_at=now,
                        input_count=(
                            step.input_count if step.input_count is not None else len(step.outcomes)
                        ),
                        output_count=output,
                        skipped_count=skipped,
                        failed_count=failed,
                    )
                    .returning(tables.pipeline_steps.c.id)
                )
            ).scalar_one()
            rows = [
                {
                    "step_id": step_id,
                    "item_kind": o.item_kind,
                    "item_ref": o.item_ref,
                    "status": o.status,
                }
                for o in _persistable(step.outcomes, verbosity)
            ]
            if rows:
                await conn.execute(tables.pipeline_step_items.insert(), rows)
    return run_id


async def purge_expired_items(conn: AsyncConnection, *, retention_days: int | None = None) -> int:
    """§18 retention (🔧 ``observability.item_retention_days``, the settings
    default when the argument is omitted): delete item ROWS whose parent run
    finished more than ``retention_days`` ago. Runs and steps (the counters)
    are kept — only the per-item detail expires; the §27.7 retry boundary
    only ever replays the LATEST run's failures, which a sane retention
    window never touches. Loaned-clean connection, like record_run."""
    if conn.in_transaction():
        raise RuntimeError("purge_expired_items requires a connection with no open transaction")
    if retention_days is None:
        from core.config import get_settings

        retention_days = get_settings().observability_item_retention_days
    if retention_days < 1:
        raise ValueError(
            "retention_days must be >= 1 — a zero window would erase "
            "the retry boundary's input as it is written"
        )
    cutoff = sa.text("now() - make_interval(days => :days)").bindparams(days=retention_days)
    async with conn.begin():
        result = await conn.execute(
            tables.pipeline_step_items.delete().where(
                tables.pipeline_step_items.c.step_id.in_(
                    sa.select(tables.pipeline_steps.c.id)
                    .join(
                        tables.pipeline_runs,
                        tables.pipeline_steps.c.run_id == tables.pipeline_runs.c.id,
                    )
                    .where(tables.pipeline_runs.c.finished_at < cutoff)
                )
            )
        )
    return int(result.rowcount or 0)
