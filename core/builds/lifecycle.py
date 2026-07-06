"""Build lifecycle operations (§14, DR-001; C9).

This is the ADMIN surface over builds — the third sanctioned store-access
face, next to the writer (binds a *building* build) and the query repos
(bind the *active* build): lifecycle operations name their build EXPLICITLY,
because the whole point of activate/rollback/diff/prune is to act on builds
that are not (yet, anymore) active. It lives in core and is consumed by the
CLI (§14) and later the Console API (BA8); query/MCP layers never import it
(DR-006 keeps them on the active-bound repos).

DR-001 invariants honored here:
- the ONE active build per project is Postgres ``builds.status='active'``
  (partial unique index) — activation and rollback are each a SINGLE
  Postgres transaction: archive the current active, promote the target,
  commit. Readers that resolved the active id before the commit keep a
  complete, consistent old snapshot; after it, the new one. Nothing is ever
  half-switched.
- preflight (§14) runs BEFORE that transaction: the target must be
  promotable (``ready``, or ``archived`` for rollback), and the three
  stores' projections must agree with Postgres on what the build contains
  (§19 drift check — counts per store), and the §20 eval gate is LIVE and
  fail-closed: an unscored candidate (or unscored active) against an
  existing active build REFUSES activation — the only vacuous cell is
  no-active-build (bootstrap); both drift and the eval gate are re-checked
  under the promotion lock.
- prune (GC) keeps the newest ``retention.keep_builds`` builds per project
  (the active build is always kept, whatever its age) and deletes everything
  older from ALL three stores by build_id — Postgres last, because its rows
  are the source of truth for what still needs deleting elsewhere.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm
from qdrant_client.http.exceptions import UnexpectedResponse
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import tables
from core.stores.vectors import collection_for

#: build-scoped Postgres tables (all carry build_id), child-before-parent
#: delete order (FKs: evidence→relations→entities; chunks→documents).
#: entity_mentions carries NO build_id — it rides on its entity FK and is
#: handled explicitly wherever these are swept.
_BUILD_TABLES: tuple[sa.Table, ...] = (
    tables.relation_evidence,
    tables.relations,
    tables.merge_candidates,
    tables.community_reports,
    tables.entities,
    tables.chunks,
    tables.documents,
)

#: the §19 drift comparison: Postgres truth vs each projection.
_DRIFT_TOLERANCE = 0

#: prune sweeps TERMINAL statuses only — active is the serving snapshot,
#: building is a live pipeline (its cancellation is not GC's job).
_PRUNABLE = ("ready", "archived", "failed")


async def _take_project_lock(conn: AsyncConnection, project: str) -> None:
    """Transaction-scoped advisory lock serializing LIFECYCLE operations per
    project (activate / rollback / prune victims). Row locks alone cannot
    order operations that first have to SELECT their target (rollback picks
    "the most recently displaced build" — a selection made outside the
    promotion's serialization can go stale and jump history). Auto-released
    at commit/rollback; hashtext collisions across projects only cause
    spurious serialization, never corruption."""
    await conn.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                sa.func.hashtext("graphrag-lifecycle"), sa.func.hashtext(project)
            )
        )
    )


class _DriftedUnderLock(Exception):
    """Raised inside the activation transaction when the post-lock drift
    re-check fails — converted to a refusal report (nothing committed)."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__("; ".join(failures))
        self.failures = failures


@dataclass(frozen=True)
class BuildInfo:
    """One row of ``graphrag builds``."""

    id: uuid.UUID
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    activated_at: datetime | None


@dataclass(frozen=True)
class PreflightReport:
    """§14 preflight: empty ``failures`` means promotable; ``deferred`` names
    checks that are genuinely inapplicable (no active build to regress
    against; rollback's history exemption) — surfaced, never silent."""

    failures: tuple[str, ...]
    deferred: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


async def list_builds(conn: AsyncConnection, project: str) -> list[BuildInfo]:
    """All builds for one project, newest first."""
    rows = await conn.execute(
        sa.select(
            tables.builds.c.id,
            tables.builds.c.status,
            tables.builds.c.started_at,
            tables.builds.c.finished_at,
            tables.builds.c.activated_at,
        )
        .where(tables.builds.c.project == project)
        .order_by(sa.desc(sa.func.coalesce(tables.builds.c.started_at, sa.func.now())))
    )
    return [BuildInfo(*row) for row in rows]


async def _pg_counts(conn: AsyncConnection, project: str, build_id: uuid.UUID) -> dict[str, int]:
    """The PROJECTED populations, not raw row counts (§19).

    The projections are built from a subset of the SoR (indexing §5): Neo4j
    nodes = ACTIVE entities; Neo4j edges = ACTIVE relations whose BOTH
    endpoints are active; Qdrant points = chunks/entities that actually got
    embedded (``*_point_id IS NOT NULL`` — a contained §22 embed failure
    leaves no point). Counting raw rows would flag every resolved build
    (merged/rejected rows stay in the SoR) as drifted — the over-block dual."""
    e, r, c = tables.entities, tables.relations, tables.chunks
    active_entities = int(
        (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(e)
                .where(e.c.project == project, e.c.build_id == build_id, e.c.status == "active")
            )
        ).scalar_one()
    )
    src, dst = e.alias("src"), e.alias("dst")
    projected_relations = int(
        (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(
                    r.join(
                        src,
                        sa.and_(
                            r.c.src_entity_id == src.c.id,
                            src.c.project == project,
                            src.c.build_id == build_id,
                            src.c.status == "active",
                        ),
                    ).join(
                        dst,
                        sa.and_(
                            r.c.dst_entity_id == dst.c.id,
                            dst.c.project == project,
                            dst.c.build_id == build_id,
                            dst.c.status == "active",
                        ),
                    )
                )
                .where(r.c.project == project, r.c.build_id == build_id, r.c.status == "active")
            )
        ).scalar_one()
    )
    chunk_points = int(
        (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(c)
                .where(c.c.build_id == build_id, c.c.vector_point_id.is_not(None))
            )
        ).scalar_one()
    )
    entity_points = int(
        (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(e)
                .where(
                    e.c.project == project,
                    e.c.build_id == build_id,
                    e.c.status == "active",
                    e.c.embedding_point_id.is_not(None),
                )
            )
        ).scalar_one()
    )
    return {
        "entities": active_entities,
        "relations": projected_relations,
        "points": chunk_points + entity_points,
    }


async def _graph_counts(session: AsyncSession, project: str, build_id: uuid.UUID) -> dict[str, int]:
    params = {"project": project, "build_id": str(build_id)}
    entity_q = "MATCH (n:Entity {project: $project, build_id: $build_id}) RETURN count(n) AS c"
    # NB: REL edges carry build_id/type but NO project property (the
    # projector's write shape; the repo's own _RELATION_COUNT mirrors this) —
    # scoping the edge by project would count zero on every real build
    relation_q = (
        "MATCH (:Entity {project: $project, build_id: $build_id})"
        "-[r:REL {build_id: $build_id}]->"
        "(:Entity {project: $project, build_id: $build_id}) RETURN count(r) AS c"
    )
    out: dict[str, int] = {}
    for key, query in (("entities", entity_q), ("relations", relation_q)):
        result = await session.run(query, params)
        record = await result.single()
        out[key] = int(record["c"]) if record else 0
    return out


async def _vector_count(client: AsyncQdrantClient, project: str, build_id: uuid.UUID) -> int:
    try:
        result = await client.count(
            collection_name=collection_for(project),
            count_filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="project", match=qm.MatchValue(value=project)),
                    qm.FieldCondition(key="build_id", match=qm.MatchValue(value=str(build_id))),
                ]
            ),
            exact=True,
        )
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return 0  # no collection yet ⇒ the projection holds zero points
        raise
    return int(result.count)


async def _drift_failures(
    conn: AsyncConnection,
    qdrant: AsyncQdrantClient,
    graph_session: AsyncSession,
    project: str,
    build_id: uuid.UUID,
) -> list[str]:
    """§19 drift: the projections must hold exactly the projected subset of
    the Postgres truth (see _pg_counts). Shared by preflight AND activate's
    post-lock re-check."""
    failures: list[str] = []
    pg = await _pg_counts(conn, project, build_id)
    graph = await _graph_counts(graph_session, project, build_id)
    if abs(pg["entities"] - graph["entities"]) > _DRIFT_TOLERANCE:
        failures.append(
            f"graph drift: postgres has {pg['entities']} entities, neo4j {graph['entities']}"
        )
    if abs(pg["relations"] - graph["relations"]) > _DRIFT_TOLERANCE:
        failures.append(
            f"graph drift: postgres has {pg['relations']} relations, neo4j {graph['relations']}"
        )
    # the vector projection holds exactly the EMBEDDED chunk+entity points
    points = await _vector_count(qdrant, project, build_id)
    if abs(points - pg["points"]) > _DRIFT_TOLERANCE:
        failures.append(
            f"vector drift: postgres implies {pg['points']} embedded points, qdrant has {points}"
        )
    return failures


async def _eval_gate(
    conn: AsyncConnection, project: str, build_id: uuid.UUID
) -> tuple[list[str], list[str]]:
    """§20's activation gate: the candidate regresses when its eval score
    falls below the ACTIVE build's by more than the threshold
    (spec.is_eval_regression — the at-threshold tolerance lives there).
    Scores come from builds.metrics['eval'] as written by the C10 runner;
    a missing score is REPORTED as deferred, never silently passed (Rule
    12): no active build or an unscored active ⇒ nothing to regress against;
    an unscored candidate ⇒ run `graphrag eval` first."""
    from core.config import get_settings
    from core.eval.spec import is_eval_regression

    async def _score(bid: uuid.UUID) -> float | None:
        row = (
            await conn.execute(sa.select(tables.builds.c.metrics).where(tables.builds.c.id == bid))
        ).one_or_none()
        if row is None or not row.metrics:
            return None
        eval_block = row.metrics.get("eval")
        if not isinstance(eval_block, dict):
            return None
        score = eval_block.get("score")
        return float(score) if isinstance(score, (int, float)) else None

    candidate = await _score(build_id)
    active_row = (
        await conn.execute(
            sa.select(tables.builds.c.id).where(
                tables.builds.c.project == project, tables.builds.c.status == "active"
            )
        )
    ).one_or_none()
    if active_row is None:
        # bootstrap: nothing to regress against — the only genuinely vacuous
        # cell (still surfaced, never silent)
        return [], ["eval gate (§20): no active build to regress against — gate vacuous"]
    if candidate is None:
        # FAIL-CLOSED (P1): deferred would be ignored by report.ok and the
        # unscored candidate would promote — bypassing the gate for exactly
        # its target case. The fix the message names is actionable.
        return [
            "eval gate (§20): candidate build has no eval score — run `graphrag eval` "
            "on it first; an unmeasured candidate cannot pass the regression gate"
        ], []
    active_score = await _score(active_row.id)
    if active_score is None:
        return [
            "eval gate (§20): the active build has no eval score — run `graphrag eval` "
            "on the active build first; the gate cannot compare against the unmeasured"
        ], []
    threshold = get_settings().eval_regression_threshold
    if is_eval_regression(candidate, active_score, threshold):
        return [
            f"eval regression (§20): candidate scored {candidate:.4f}, active "
            f"{active_score:.4f}, threshold {threshold} — activation blocked"
        ], []
    return [], []


async def preflight(
    conn: AsyncConnection,
    qdrant: AsyncQdrantClient,
    graph_session: AsyncSession,
    project: str,
    build_id: uuid.UUID,
    *,
    allow_archived: bool = False,
) -> PreflightReport:
    """§14 activation preflight, run OUTSIDE the activation transaction.

    Checks: (1) the target exists and is promotable — ``ready`` for a fresh
    activation, additionally ``archived`` for a rollback (``allow_archived``);
    (2) §19 drift — the graph and vector projections agree with the Postgres
    truth on entity/relation/point counts for THIS build; (3) the §20 eval
    gate, FAIL-CLOSED: unscored candidate/active with an active present →
    FAILURE; no active build → vacuous (deferred); both scored →
    regression blocks. Both the drift check AND the eval gate are
    re-checked under the promotion lock (racing activations can replace the
    active build between preflight and lock — the comparison must bind to
    the active at promotion time); rollback is exempt from the eval gate
    (it restores an already-vetted build)."""
    failures: list[str] = []
    row = (
        await conn.execute(
            sa.select(tables.builds.c.status).where(
                tables.builds.c.id == build_id, tables.builds.c.project == project
            )
        )
    ).one_or_none()
    if row is None:
        return PreflightReport((f"build {build_id} not found in project {project}",), ())
    status = row.status
    promotable = ("ready", "archived") if allow_archived else ("ready",)
    if status == "active":
        failures.append("build is already active")
    elif status not in promotable:
        failures.append(
            f"build status is '{status}' — promotable statuses here: {', '.join(promotable)}"
        )

    failures.extend(await _drift_failures(conn, qdrant, graph_session, project, build_id))

    eval_failures, eval_deferred = await _eval_gate(conn, project, build_id)
    failures.extend(eval_failures)
    return PreflightReport(tuple(failures), tuple(eval_deferred))


async def activate(
    conn: AsyncConnection,
    qdrant: AsyncQdrantClient,
    graph_session: AsyncSession,
    project: str,
    build_id: uuid.UUID,
    *,
    allow_archived: bool = False,
) -> PreflightReport:
    """Preflight, then promote ``build_id`` in ONE transaction (DR-001).

    The transaction archives the currently active build (if any) and promotes
    the target; the partial unique index on ``builds`` makes a concurrent
    second activation lose loudly rather than produce two actives. On
    preflight failure NOTHING is changed — the report says why."""
    report = await preflight(
        conn, qdrant, graph_session, project, build_id, allow_archived=allow_archived
    )
    if not report.ok:
        return report
    # preflight's SELECTs auto-began a read transaction — end it so the
    # activation transaction below is the connection's ONLY one (the same
    # explicit-rollback idiom as the MCP binding path)
    await conn.rollback()
    promotable = ("ready", "archived") if allow_archived else ("ready",)
    try:
        async with conn.begin():
            await _take_project_lock(conn, project)
            return await _promote_in_tx(
                conn, qdrant, graph_session, project, build_id, promotable, report
            )
    except _DriftedUnderLock as exc:
        return PreflightReport(tuple(exc.failures), report.deferred)


async def _promote_in_tx(
    conn: AsyncConnection,
    qdrant: AsyncQdrantClient,
    graph_session: AsyncSession,
    project: str,
    build_id: uuid.UUID,
    promotable: tuple[str, ...],
    report: PreflightReport,
    *,
    apply_eval_gate: bool = True,
) -> PreflightReport:
    """The promotion body — runs inside the CALLER's transaction, which must
    already hold the project lifecycle lock (activate/rollback both do).
    ``apply_eval_gate=False`` is rollback's path: it restores a previously
    active, already-vetted build — the regression gate compares candidates,
    not history."""
    # take the target's row lock FIRST: this serializes against prune
    # (which sweeps under the same lock) — if prune committed, the row
    # is gone; if prune ABORTED after deleting the projections (the
    # crash window), the row is back but the projections may not be
    locked = (
        await conn.execute(
            sa.select(tables.builds.c.status)
            .where(tables.builds.c.id == build_id, tables.builds.c.project == project)
            .with_for_update()
        )
    ).one_or_none()
    if locked is None or locked.status not in promotable:
        raise RuntimeError(
            f"activation lost the race: build {build_id} was no longer promotable "
            "inside the transaction — nothing committed"
        )
    # re-run the DRIFT check under the lock (post-lock preflight): the
    # pre-lock preflight is bind-time knowledge — an aborted prune can
    # have deleted the projections between it and this lock, and
    # promoting then would point active at missing projections
    drift = await _drift_failures(conn, qdrant, graph_session, project, build_id)
    if drift:
        raise _DriftedUnderLock(drift)
    if apply_eval_gate:
        # re-run the §20 gate UNDER the lock too (P2): two racing scored
        # activations — the pre-lock preflight compared against an active
        # that another activation may have replaced by the time we hold the
        # lock; the comparison must bind to the active AT PROMOTION TIME.
        # PG-only reads — cheap, unlike the drift check's external stores.
        eval_failures, _eval_deferred = await _eval_gate(conn, project, build_id)
        if eval_failures:
            raise _DriftedUnderLock(eval_failures)
    await conn.execute(
        tables.builds.update()
        .where(tables.builds.c.project == project, tables.builds.c.status == "active")
        .values(
            status="archived",
            # a build created DIRECTLY as active has activated_at NULL —
            # backfill the displacement moment so rollback's ordering (most
            # recently displaced first) stays monotonic: activation order ==
            # displacement order for the normal chain, and this keeps the
            # NULL case inside that chain instead of falling back to a
            # possibly ancient started_at
            activated_at=sa.func.coalesce(tables.builds.c.activated_at, sa.func.now()),
        )
    )
    promoted = await conn.execute(
        tables.builds.update()
        .where(
            tables.builds.c.id == build_id,
            tables.builds.c.project == project,
            tables.builds.c.status.in_(promotable),  # belt: the lock is the mechanism
        )
        .values(
            # PG's clock, same source as the archive backfill — mixing the
            # application clock in would let skew reorder displacement history
            status="active",
            activated_at=sa.func.now(),
        )
    )
    if promoted.rowcount != 1:
        raise RuntimeError(
            f"activation lost the race: build {build_id} was no longer promotable "
            "inside the transaction — nothing committed"
        )
    return report


async def rollback(
    conn: AsyncConnection,
    qdrant: AsyncQdrantClient,
    graph_session: AsyncSession,
    project: str,
) -> tuple[uuid.UUID | None, PreflightReport]:
    """Activate the most recently PREVIOUSLY-active build (§14: instant,
    atomic — it is an activation of an archived build).

    Target selection happens INSIDE the same transaction — and under the
    same project lifecycle lock — as the promotion: selected outside it, a
    concurrent activation could commit in between and the pre-selected
    target would jump history back two versions instead of one (bind-time
    selection ≠ invariant)."""
    deferred = (
        "eval gate (§20) not applied — rollback restores a previously-active, "
        "already-vetted build; the regression gate compares candidates, not history",
    )
    await conn.rollback()  # end any auto-begun read txn before OUR txn
    try:
        async with conn.begin():
            await _take_project_lock(conn, project)
            # archived ⇒ was displaced from active (activation is the ONLY
            # archiving path in this lifecycle); activated_at can be NULL for
            # builds created directly as active — order falls back to
            # started_at for those rows. Stable under the project lock.
            row = (
                await conn.execute(
                    sa.select(tables.builds.c.id)
                    .where(
                        tables.builds.c.project == project,
                        tables.builds.c.status == "archived",
                    )
                    .order_by(
                        sa.desc(
                            sa.func.coalesce(
                                tables.builds.c.activated_at,
                                tables.builds.c.started_at,
                                sa.func.now(),
                            )
                        ),
                        sa.desc(tables.builds.c.id),
                    )
                    .limit(1)
                )
            ).one_or_none()
            if row is None:
                return None, PreflightReport(
                    ("no previously-active build to roll back to",), deferred
                )
            report = await _promote_in_tx(
                conn,
                qdrant,
                graph_session,
                project,
                row.id,
                ("ready", "archived"),
                PreflightReport((), deferred),
                apply_eval_gate=False,
            )
            return row.id, report
    except _DriftedUnderLock as exc:
        return None, PreflightReport(tuple(exc.failures), deferred)


async def diff(
    conn: AsyncConnection, project: str, build_a: uuid.UUID, build_b: uuid.UUID
) -> dict[str, dict[str, int]]:
    """Row-count delta per build-scoped table between two builds (the CLI's
    ``graphrag diff``): {table: {a, b, delta}}.

    Both builds must belong to ``project`` — the build-only tables (chunks,
    relation_evidence) are scoped by build_id alone, so a foreign build id
    (typo, copied uuid) would otherwise produce a MIXED cross-project diff
    instead of a refusal."""
    known = {
        row.id
        for row in await conn.execute(
            sa.select(tables.builds.c.id).where(
                tables.builds.c.project == project,
                tables.builds.c.id.in_([build_a, build_b]),
            )
        )
    }
    for label, build in (("a", build_a), ("b", build_b)):
        if build not in known:
            raise ValueError(f"build {label}={build} does not belong to project {project}")
    out: dict[str, dict[str, int]] = {}
    for table in reversed(_BUILD_TABLES):  # parent-first reads nicer
        counts: dict[str, int] = {}
        for label, build in (("a", build_a), ("b", build_b)):
            scope = [table.c.build_id == build]
            if "project" in table.c:
                scope.append(table.c.project == project)
            result = await conn.execute(sa.select(sa.func.count()).select_from(table).where(*scope))
            counts[label] = int(result.scalar_one())
        out[table.name] = {**counts, "delta": counts["b"] - counts["a"]}
    mention_counts: dict[str, int] = {}
    for label, build in (("a", build_a), ("b", build_b)):
        result = await conn.execute(
            sa.select(sa.func.count())
            .select_from(tables.entity_mentions.join(tables.entities))
            .where(
                tables.entities.c.project == project,
                tables.entities.c.build_id == build,
            )
        )
        mention_counts[label] = int(result.scalar_one())
    out["entity_mentions"] = {
        **mention_counts,
        "delta": mention_counts["b"] - mention_counts["a"],
    }
    return out


async def prune(
    conn: AsyncConnection,
    qdrant: AsyncQdrantClient,
    graph_session: AsyncSession,
    project: str,
    *,
    keep: int,
) -> list[uuid.UUID]:
    """§14 GC: keep the newest ``keep`` builds (the ACTIVE build is always
    kept regardless of age); delete every older build's data from all three
    stores — projections first, Postgres LAST (its rows are the truth for
    what still needs deleting elsewhere; a crash mid-prune leaves re-runnable
    leftovers, never orphaned truth). Each victim's builds row is locked
    FOR UPDATE and its status RE-CHECKED inside the one deleting transaction
    (gone/active → skipped, data kept): a concurrent activation of that build
    blocks on the lock and then loses LOUD (no promotable row). No status is
    written; a crash rolls the Postgres truth back — the victim stays
    archived and the next run re-sweeps it."""
    if keep < 1:
        raise ValueError("keep must be >= 1 — pruning everything would drop the active build")
    builds = await list_builds(conn, project)
    keepers = {b.id for b in builds[:keep]} | {b.id for b in builds if b.status == "active"}
    # only TERMINAL statuses are prunable: a 'building' row is a LIVE build —
    # sweeping it would delete truth and partial outputs from under the
    # pipeline writer (cancellation is that surface's job, not GC's)
    victims = [b for b in builds if b.id not in keepers and b.status in _PRUNABLE]
    pruned: list[uuid.UUID] = []
    for victim in victims:
        # the snapshot above is bind-time knowledge, not an invariant: a
        # concurrent activation could promote a victim before we reach it
        # (class 10). Serialization is the victim's ROW LOCK: one transaction
        # covers recheck + projection deletes + Postgres deletes, so a
        # concurrent activation of THIS build blocks on its promote UPDATE
        # until our commit — and then finds no promotable row (deleted) and
        # fails loud, never a silent double-state. A crash anywhere inside
        # rolls the Postgres truth back intact; projections partially
        # deleted are re-runnable leftovers (idempotent deletes).
        await conn.rollback()  # end any auto-begun read txn before OUR txn
        # NB the row lock is held across the external-store deletes below; a
        # hung store pins this transaction (idle-in-transaction) — bounded
        # blast radius (only activation of THIS archived victim contends),
        # but a session lock_timeout/statement_timeout is the mitigation if
        # this ever runs unattended
        async with conn.begin():
            await _take_project_lock(conn, project)
            locked = (
                await conn.execute(
                    sa.select(tables.builds.c.status)
                    .where(tables.builds.c.id == victim.id)
                    .with_for_update()
                )
            ).one_or_none()
            if locked is None or locked.status not in _PRUNABLE:
                # gone, promoted, or a build that (re)started since the
                # snapshot — never sweep a non-terminal status
                continue
            # the row lock is HELD through everything below: projections
            # first, Postgres truth last, one commit releases it
            try:
                await qdrant.delete(
                    collection_name=collection_for(project),
                    points_selector=qm.FilterSelector(
                        filter=qm.Filter(
                            must=[
                                qm.FieldCondition(
                                    key="project", match=qm.MatchValue(value=project)
                                ),
                                qm.FieldCondition(
                                    key="build_id", match=qm.MatchValue(value=str(victim.id))
                                ),
                            ]
                        )
                    ),
                )
            except UnexpectedResponse as exc:
                if exc.status_code != 404:  # no collection ⇒ nothing to prune there
                    raise
            await (
                await graph_session.run(
                    "MATCH (n:Entity {project: $project, build_id: $build_id}) DETACH DELETE n",
                    {"project": project, "build_id": str(victim.id)},
                )
            ).consume()
            # Postgres last, children before parents, the build row at the end
            # (entity_mentions has no build_id — resolve through its entity FK)
            await conn.execute(
                tables.entity_mentions.delete().where(
                    tables.entity_mentions.c.entity_id.in_(
                        sa.select(tables.entities.c.id).where(
                            tables.entities.c.project == project,
                            tables.entities.c.build_id == victim.id,
                        )
                    )
                )
            )
            for table in _BUILD_TABLES:
                scope = [table.c.build_id == victim.id]
                if "project" in table.c:
                    scope.append(table.c.project == project)
                await conn.execute(table.delete().where(*scope))
            await conn.execute(
                tables.pipeline_runs.delete().where(tables.pipeline_runs.c.build_id == victim.id)
            )
            await conn.execute(tables.builds.delete().where(tables.builds.c.id == victim.id))
            pruned.append(victim.id)
    return pruned
