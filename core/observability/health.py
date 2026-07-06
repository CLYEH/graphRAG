"""Project Health (§19; C11) — the report `GET /projects/{p}/health` serves.

SEMANTICS (spec-first — the judge-surface lesson):

- **Status light precedence** (§19 names five lights; when several
  conditions hold, ONE wins — most actionable first):
  ``Build failed`` > ``Index drift`` > ``Eval regression`` >
  ``Needs review`` > ``Healthy``.
  * Build failed — the MOST RECENT build (by started_at) has
    ``status='failed'``: the operator's next action is fixing the pipeline,
    which supersedes everything else.
  * Index drift — the ACTIVE build's projections disagree with the SoR;
    the check is lifecycle's own ``drift_failures`` (ONE checker for
    preflight and Health — a fork here is the class-5 checker/consumer
    split). No active build → the check is skipped, never counted as drift.
  * Eval regression — §20 verbatim: the newest READY build's eval score
    regresses vs the active's, on COMPARABLE reports (same fingerprint —
    incomparable or unscored is NOT a regression light; the activation gate
    fails closed there, but a status light must not scream about what was
    never measured).
  * Needs review — pending merge candidates exist
    (``merge_candidates.status='pending'``).
- **Metrics** are point-in-time counts, active-build-scoped where the metric
  is about content (docs/chunks/entities/relations), project-scoped where it
  is about workflow (builds, pending review). ``low_confidence_relations``
  counts ACTIVE relations with ``confidence < 0.5`` (🔧 threshold param);
  ``missing_evidence_relations`` counts ACTIVE relations with zero evidence
  rows — both §19-named quality signals.
- **Read-only**: Health never mutates; it binds the active build the same
  way the admin surface does (explicit lookups — Health must report on
  BROKEN states, e.g. drift, that the query repos' fences exist to hide).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncConnection

from core.builds.lifecycle import drift_failures, list_builds
from core.config import get_settings
from core.eval.spec import is_eval_regression
from core.stores import tables

STATUS_LIGHTS = (
    "Build failed",
    "Index drift",
    "Eval regression",
    "Needs review",
    "Healthy",
)


@dataclass(frozen=True)
class HealthReport:
    project: str
    status: str
    active_build_id: uuid.UUID | None
    drift: tuple[str, ...]
    metrics: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "status": self.status,
            "active_build_id": str(self.active_build_id) if self.active_build_id else None,
            "drift": list(self.drift),
            "metrics": self.metrics,
        }


async def _count(conn: AsyncConnection, query: sa.Select[Any]) -> int:
    return int((await conn.execute(query)).scalar_one())


async def health_report(
    conn: AsyncConnection,
    qdrant: AsyncQdrantClient,
    graph_session: AsyncSession,
    project: str,
    *,
    low_confidence_below: float = 0.5,
) -> HealthReport:
    """Compute §19's report for one project (read-only)."""
    builds = await list_builds(conn, project)
    active = next((b for b in builds if b.status == "active"), None)
    newest = builds[0] if builds else None
    last_failed = next((b for b in builds if b.status == "failed"), None)

    drift: tuple[str, ...] = ()
    if active is not None:
        drift = tuple(await drift_failures(conn, qdrant, graph_session, project, active.id))

    e, r, ev = tables.entities, tables.relations, tables.relation_evidence
    metrics: dict[str, Any] = {
        "builds_total": len(builds),
        "active_build": str(active.id) if active else None,
        "last_failed_build": str(last_failed.id) if last_failed else None,
        "pending_review": await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(tables.merge_candidates)
            .where(
                tables.merge_candidates.c.project == project,
                tables.merge_candidates.c.status == "pending",
            ),
        ),
    }
    if active is not None:
        scope_e = [e.c.project == project, e.c.build_id == active.id, e.c.status == "active"]
        scope_r = [r.c.project == project, r.c.build_id == active.id, r.c.status == "active"]
        metrics["documents"] = await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(tables.documents)
            .where(tables.documents.c.project == project, tables.documents.c.build_id == active.id),
        )
        metrics["chunks"] = await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(tables.chunks)
            .where(tables.chunks.c.build_id == active.id),
        )
        metrics["entities"] = await _count(
            conn, sa.select(sa.func.count()).select_from(e).where(*scope_e)
        )
        metrics["relations"] = await _count(
            conn, sa.select(sa.func.count()).select_from(r).where(*scope_r)
        )
        metrics["low_confidence_relations"] = await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(r)
            .where(*scope_r, r.c.confidence < low_confidence_below),
        )
        metrics["missing_evidence_relations"] = await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(r)
            .where(
                *scope_r,
                ~sa.exists(sa.select(sa.literal(1)).where(ev.c.relation_id == r.c.id)),
            ),
        )
        eval_block = (
            await conn.execute(
                sa.select(tables.builds.c.eval).where(tables.builds.c.id == active.id)
            )
        ).scalar_one_or_none()
        if isinstance(eval_block, dict):
            metrics["eval"] = {
                "score": eval_block.get("score"),
                "passed": eval_block.get("passed"),
                "failed": eval_block.get("failed"),
            }

    status = status_light(
        newest_failed=newest is not None and newest.status == "failed",
        drift=bool(drift),
        eval_regressed=active is not None and await _eval_regressed(conn, project, active.id),
        pending_review=metrics["pending_review"] > 0,
    )

    return HealthReport(
        project=project,
        status=status,
        active_build_id=active.id if active else None,
        drift=drift,
        metrics=metrics,
    )


def status_light(
    *, newest_failed: bool, drift: bool, eval_regressed: bool, pending_review: bool
) -> str:
    """§19's ONE light from the four conditions — precedence exactly as the
    module docstring specifies (most actionable wins): Build failed >
    Index drift > Eval regression > Needs review > Healthy."""
    if newest_failed:
        return "Build failed"
    if drift:
        return "Index drift"
    if eval_regressed:
        return "Eval regression"
    if pending_review:
        return "Needs review"
    return "Healthy"


async def _eval_regressed(conn: AsyncConnection, project: str, active_id: uuid.UUID) -> bool:
    """§20's light: the newest READY build regresses vs the active on
    COMPARABLE (same-fingerprint) eval reports. Unscored or incomparable is
    NOT a regression — the light reports a measured fact, never a guess."""
    rows = (
        await conn.execute(
            sa.select(tables.builds.c.id, tables.builds.c.eval)
            .where(tables.builds.c.project == project, tables.builds.c.status == "ready")
            .order_by(sa.desc(sa.func.coalesce(tables.builds.c.started_at, sa.func.now())))
            .limit(1)
        )
    ).one_or_none()
    if rows is None or not isinstance(rows.eval, dict):
        return False
    active_eval = (
        await conn.execute(sa.select(tables.builds.c.eval).where(tables.builds.c.id == active_id))
    ).scalar_one_or_none()
    if not isinstance(active_eval, dict):
        return False
    candidate_score, active_score = rows.eval.get("score"), active_eval.get("score")
    candidate_fp, active_fp = rows.eval.get("fingerprint"), active_eval.get("fingerprint")
    if not isinstance(candidate_score, (int, float)) or not isinstance(active_score, (int, float)):
        return False
    if not isinstance(candidate_fp, str) or candidate_fp != active_fp:
        return False  # incomparable suites — the gate fails closed, the light stays honest
    return is_eval_regression(
        float(candidate_score), float(active_score), get_settings().eval_regression_threshold
    )
