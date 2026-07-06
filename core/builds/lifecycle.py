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
  (§19 drift check — counts per store). The eval gate (§20: eval score ≥
  threshold vs the active build) needs the C10 eval harness and is a
  DOCUMENTED DEFERRAL: preflight reports it as unchecked rather than
  silently passing it.
- prune (GC) keeps the newest ``retention.keep_builds`` builds per project
  (the active build is always kept, whatever its age) and deletes everything
  older from ALL three stores by build_id — Postgres last, because its rows
  are the source of truth for what still needs deleting elsewhere.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

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
    the checks this codebase cannot run yet (eval gate → C10) — surfaced,
    never silently passed (Rule 12)."""

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
    truth on entity/relation/point counts for THIS build. The §20 eval gate
    is deferred to C10 and reported as such."""
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

    return PreflightReport(
        tuple(failures),
        ("eval gate (§20) not run — the eval harness lands in C10",),
    )


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
    async with conn.begin():
        await conn.execute(
            tables.builds.update()
            .where(tables.builds.c.project == project, tables.builds.c.status == "active")
            .values(status="archived")
        )
        promoted = await conn.execute(
            tables.builds.update()
            .where(
                tables.builds.c.id == build_id,
                tables.builds.c.project == project,
                # re-check INSIDE the transaction: the status may have moved
                # since preflight (bind-time check ≠ invariant — class 10)
                tables.builds.c.status.in_(("ready", "archived") if allow_archived else ("ready",)),
            )
            .values(status="active", activated_at=datetime.now(tz=UTC))
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
    atomic — it is just an activation of an archived build)."""
    # archived ⇒ was displaced from active (activation is the ONLY archiving
    # path in this lifecycle), so the predicate must not demand activated_at:
    # a build created directly as active (schema-legal) archives with it
    # still NULL and would otherwise vanish as a rollback target. Ordering
    # falls back to started_at for those rows.
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
        return None, PreflightReport(("no previously-active build to roll back to",), ())
    report = await activate(conn, qdrant, graph_session, project, row.id, allow_archived=True)
    return row.id, report


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
    leftovers, never orphaned truth)."""
    if keep < 1:
        raise ValueError("keep must be >= 1 — pruning everything would drop the active build")
    # single-operator admin surface: the keeper set is a snapshot — prune
    # assumes no CONCURRENT activation (a build activated after the snapshot
    # could be swept); serialize lifecycle operations per project
    builds = await list_builds(conn, project)
    keepers = {b.id for b in builds[:keep]} | {b.id for b in builds if b.status == "active"}
    victims = [b for b in builds if b.id not in keepers]
    for victim in victims:
        # projections first
        try:
            await qdrant.delete(
                collection_name=collection_for(project),
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[
                            qm.FieldCondition(key="project", match=qm.MatchValue(value=project)),
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
        await conn.rollback()  # end any auto-begun read txn before OUR txn
        async with conn.begin():
            # entity_mentions has no build_id — resolve through its entity FK
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
    return [v.id for v in victims]
