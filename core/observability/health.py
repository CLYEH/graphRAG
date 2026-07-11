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
    The probe touches Neo4j/Qdrant, so it is (1) SKIPPED entirely when the
    newest build already failed — Build failed outranks drift, and the
    operator's answer must not depend on a projection store being up — and
    (2) degraded to a STORE_UNAVAILABLE warning when a store raises: an
    unreachable store is UNMEASURED drift, not drift (the same
    only-report-measured-facts rule the eval light follows).
  * Eval regression — §20 verbatim: the newest READY build's eval score
    regresses vs the active's, on COMPARABLE reports (same fingerprint —
    incomparable or unscored is NOT a regression light; the activation gate
    fails closed there, but a status light must not scream about what was
    never measured).
  * Needs review — the review QUEUE is non-empty. §17 defines the whole
    queue, and every one of its pending states counts: merge candidates
    ``status IN ('pending','deferred')`` (defer 仍列入待審), proposed
    ontology types (``ontology_proposals.status='proposed'``, the §6 待審
    池), and entity/relation rows parked at ``status='needs_review'``.
    Any one of them non-empty lights Needs review — "pending review" is the
    whole §17 queue, not one table.
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from neo4j import AsyncDriver
from neo4j.exceptions import DriverError, Neo4jError
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncConnection

from core.builds.lifecycle import drift_failures, list_builds
from core.config import get_settings
from core.eval.spec import is_eval_regression
from core.stores import tables

#: what the drift probe can raise per store (mirrors the MCP layer's
#: _STORE_ERRORS): unreachable = unmeasured, degraded to a warning.
_PROBE_ERRORS = (Neo4jError, DriverError, ApiException)

STATUS_LIGHTS = (
    "Build failed",
    "Index drift",
    "Eval regression",
    "Needs review",
    "Healthy",
)

#: §19's display names → the FROZEN HealthStatus enum (openapi.yaml, lower
#: snake_case). to_payload speaks the contract; the display strings are the
#: Console's concern.
_CONTRACT_STATUS = {
    "Build failed": "build_failed",
    "Index drift": "index_drift",
    "Eval regression": "eval_regression",
    "Needs review": "needs_review",
    "Healthy": "healthy",
}


@dataclass(frozen=True)
class HealthReport:
    project: str
    status: str
    active_build_id: uuid.UUID | None
    drift: tuple[str, ...]
    metrics: dict[str, Any]
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """The FROZEN HealthReport contract shape (openapi.yaml): status is
        the lower-snake HealthStatus enum, ``drift`` is object-or-null
        (null = no drift; details keyed when present), counts/pending_review
        as typed. Extra keys ride under additionalProperties: true."""
        return {
            "project": self.project,
            "status": _CONTRACT_STATUS[self.status],
            "active_build_id": str(self.active_build_id) if self.active_build_id else None,
            "drift": {"failures": list(self.drift)} if self.drift else None,
            "pending_review": int(self.metrics.get("pending_review", 0)),
            "counts": {
                key: value
                for key, value in self.metrics.items()
                if isinstance(value, int) and key != "pending_review"
            },
            "warnings": [
                {"code": "STORE_UNAVAILABLE", "message": message} for message in self.warnings
            ],
            "metrics": self.metrics,
        }


async def _count(conn: AsyncConnection, query: sa.Select[Any]) -> int:
    return int((await conn.execute(query)).scalar_one())


def _score(value: Any) -> float | None:
    """An eval score, or None when unscored/malformed. bool is checked FIRST:
    it subclasses int, and a boolean score must read as UNSCORED, never as
    1.0/0.0 in the §20 comparison (Codex #62). ONE definition for both
    readers (the light and the endpoint) — a fork here is a class-5 split."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


async def health_report(
    conn: AsyncConnection,
    project: str,
    *,
    vector_provider: Callable[[], Awaitable[AsyncQdrantClient]],
    graph_provider: Callable[[], Awaitable[AsyncDriver]],
    low_confidence_below: float = 0.5,
) -> HealthReport:
    """Compute §19's report for one project (read-only).

    The projection stores arrive as PROVIDERS, acquired ONLY when the drift
    probe actually runs — a missing/bootstrap project or a failed-newest
    build must answer without ever touching Neo4j/Qdrant construction or
    config (the #53 R3 eager-acquisition class; Codex #62). The Neo4j
    session opens here, scoped to the probe, and closes with it."""
    builds = await list_builds(conn, project)
    active = next((b for b in builds if b.status == "active"), None)
    newest = builds[0] if builds else None
    last_failed = next((b for b in builds if b.status == "failed"), None)

    newest_failed = newest is not None and newest.status == "failed"
    drift: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    if active is not None and not newest_failed:
        # skipped when Build failed already wins: the operator's answer must
        # not depend on Neo4j/Qdrant being reachable (Codex round 6)
        try:
            qdrant = await vector_provider()
            driver = await graph_provider()
            async with driver.session() as graph_session:
                drift = tuple(await drift_failures(conn, qdrant, graph_session, project, active.id))
        except _PROBE_ERRORS as exc:
            # unreachable store = UNMEASURED drift, not drift — degrade to
            # the frozen STORE_UNAVAILABLE warning instead of a 500
            warnings = (f"drift check unavailable: {exc.__class__.__name__}",)

    e, r, ev = tables.entities, tables.relations, tables.relation_evidence
    metrics: dict[str, Any] = {
        "builds_total": len(builds),
        "active_build": str(active.id) if active else None,
        "last_failed_build": str(last_failed.id) if last_failed else None,
        "pending_merge_candidates": await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(tables.merge_candidates)
            .where(
                tables.merge_candidates.c.project == project,
                # defer 仍列入待審 (§17) — deferred is still review work
                tables.merge_candidates.c.status.in_(("pending", "deferred")),
            ),
        ),
        "pending_ontology_proposals": await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(tables.ontology_proposals)
            .where(
                tables.ontology_proposals.c.project == project,
                tables.ontology_proposals.c.status == "proposed",
            ),
        ),
        "needs_review_entities": await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(tables.entities)
            .where(
                tables.entities.c.project == project,
                tables.entities.c.status == "needs_review",
            ),
        ),
        "needs_review_relations": await _count(
            conn,
            sa.select(sa.func.count())
            .select_from(tables.relations)
            .where(
                tables.relations.c.project == project,
                tables.relations.c.status == "needs_review",
            ),
        ),
    }
    # §19's "pending review" is the WHOLE §17 queue — ANY of its pending
    # states alone must light Needs review (Codex rounds 4/8: a
    # proposal-only or needs_review-only backlog was hidden)
    metrics["pending_review"] = (
        metrics["pending_merge_candidates"]
        + metrics["pending_ontology_proposals"]
        + metrics["needs_review_entities"]
        + metrics["needs_review_relations"]
    )
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
        newest_failed=newest_failed,
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
        warnings=warnings,
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


async def latest_eval_payload(conn: AsyncConnection, project: str) -> dict[str, Any]:
    """§20's ``GET /projects/{p}/eval`` payload — the LATEST eval report.

    "Latest" = the newest build (started_at desc, NULLS LAST — the same
    ordering lesson as `_eval_regressed`) carrying an eval block; none →
    all-null report (the light's measured-facts rule: absence is reported,
    never invented). Field mapping onto the FROZEN EvalReport:

    - ``passed`` (boolean|null) — the same predicate the §14 activation gate
      and the CLI use: a report with ``failed > 0`` per-case min_score
      misses did NOT pass; a malformed/absent count is null, not a guess.
      The stored per-case COUNTS ride as ``cases_passed``/``cases_failed``
      (additionalProperties) — reusing the stored key ``passed`` would clash
      an int into the contract's boolean.
    - ``regression`` (boolean|null) — the §20 comparison against the ACTIVE
      build's report, exactly as the gate/light compute it (numeric scores +
      same fingerprint → ``is_eval_regression``); vacuous (the served build
      IS the active, no active, unscored either side, or incomparable
      fingerprints) → null."""
    rows = (
        await conn.execute(
            sa.select(tables.builds.c.id, tables.builds.c.eval)
            .where(tables.builds.c.project == project, tables.builds.c.eval.isnot(None))
            .order_by(sa.desc(tables.builds.c.started_at).nulls_last())
        )
    ).all()
    served = next((row for row in rows if isinstance(row.eval, dict)), None)
    if served is None:
        return {"build_id": None, "passed": None, "regression": None, "metrics": {}}
    block: dict[str, Any] = served.eval
    failed = block.get("failed")
    # type-is, not isinstance: bool subclasses int, and a malformed
    # {"failed": false} must be null, never a passing report (Codex #62)
    passed = (failed == 0) if type(failed) is int else None

    regression: bool | None = None
    active_row = (
        await conn.execute(
            sa.select(tables.builds.c.id, tables.builds.c.eval).where(
                tables.builds.c.project == project, tables.builds.c.status == "active"
            )
        )
    ).one_or_none()
    if active_row is not None and active_row.id != served.id and isinstance(active_row.eval, dict):
        served_score, active_score = (
            _score(block.get("score")),
            _score(active_row.eval.get("score")),
        )
        served_fp, active_fp = block.get("fingerprint"), active_row.eval.get("fingerprint")
        if (
            served_score is not None
            and active_score is not None
            and isinstance(served_fp, str)
            and served_fp == active_fp
        ):
            regression = is_eval_regression(
                served_score, active_score, get_settings().eval_regression_threshold
            )

    metrics = block.get("metrics")
    return {
        "build_id": str(served.id),
        "passed": passed,
        "regression": regression,
        "metrics": metrics if isinstance(metrics, dict) else {},
        "score": block.get("score"),
        "fingerprint": block.get("fingerprint"),
        "cases_passed": block.get("passed"),
        "cases_failed": block.get("failed"),
    }


async def _eval_regressed(conn: AsyncConnection, project: str, active_id: uuid.UUID) -> bool:
    """§20's light: the newest READY build regresses vs the active on
    COMPARABLE (same-fingerprint) eval reports. Unscored or incomparable is
    NOT a regression — the light reports a measured fact, never a guess."""
    rows = (
        await conn.execute(
            sa.select(tables.builds.c.id, tables.builds.c.eval)
            .where(tables.builds.c.project == project, tables.builds.c.status == "ready")
            # NULLS LAST: a never-started row must sort OLDEST — coalesce to
            # now() made it outrank every real timestamp and hide a newer
            # regressing candidate (Codex round 6)
            .order_by(sa.desc(tables.builds.c.started_at).nulls_last())
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
    candidate_score, active_score = _score(rows.eval.get("score")), _score(active_eval.get("score"))
    candidate_fp, active_fp = rows.eval.get("fingerprint"), active_eval.get("fingerprint")
    if candidate_score is None or active_score is None:
        return False  # unscored (a boolean score included) is not a regression
    if not isinstance(candidate_fp, str) or candidate_fp != active_fp:
        return False  # incomparable suites — the gate fails closed, the light stays honest
    return is_eval_regression(
        candidate_score, active_score, get_settings().eval_regression_threshold
    )
