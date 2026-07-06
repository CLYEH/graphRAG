"""Why: activation is DR-001's write side — §14's promises are (1) preflight
refuses an unpromotable or drifted build BEFORE anything changes, (2) the
switch itself is one atomic transaction (never two actives, never half a
switch), (3) rollback is just activation of the previously-active build, and
(4) prune never deletes the active build and sweeps ALL three stores. These
tests encode each promise against live stores; the unit half pins the pure
logic (report semantics, guardrails, CLI exit codes).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from cli.main import _parser, _print_report
from core.builds.lifecycle import (
    PreflightReport,
    activate,
    diff,
    list_builds,
    preflight,
    prune,
    rollback,
)
from core.config import get_settings
from core.resolve import fingerprints
from core.stores import tables
from core.stores.graph import graph_driver
from core.stores.repo import BuildScopedWriter
from core.stores.vectors import vector_client

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)


# ---------------------------------------------------------------- unit ----


def test_preflight_report_semantics() -> None:
    """ok == no failures; deferred checks alone do NOT block (they are
    surfaced, §20's eval gate waits for C10 — but must not freeze the §14
    lifecycle until then)."""
    assert PreflightReport((), ("eval gate not run",)).ok
    assert not PreflightReport(("drift",), ()).ok


def test_print_report_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 12 at the shell: a refused operation exits 1 (scripts can gate),
    an ok one exits 0 with the deferrals still printed for the operator."""
    assert _print_report(PreflightReport((), ("eval gate not run",))) == 0
    assert _print_report(PreflightReport(("no",), ())) == 1
    err = capsys.readouterr().err
    assert "deferred: eval gate not run" in err and "REFUSED: no" in err


def test_cli_surface_is_the_frozen_14_set() -> None:
    """§14 names the CLI verbs — the parser exposes exactly the lifecycle
    subset owned by C9 (build/ingest land with their own tracks)."""
    sub = next(
        a for a in _parser()._actions if isinstance(a, __import__("argparse")._SubParsersAction)
    )
    assert set(sub.choices) == {"builds", "activate", "rollback", "diff", "prune"}


async def test_prune_refuses_a_zero_window() -> None:
    with pytest.raises(ValueError, match="keep must be >= 1"):
        await prune(None, None, None, "p", keep=0)  # type: ignore[arg-type]


# ---------------------------------------------------------- integration ----


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def project(migrated: None) -> AsyncIterator[str]:
    name = f"lifec-{uuid.uuid4().hex[:10]}"
    yield name
    engine = _engine()
    async with engine.connect() as conn:
        await conn.execute(tables.entities.delete().where(tables.entities.c.project == name))
        await conn.execute(tables.builds.delete().where(tables.builds.c.project == name))
        await conn.commit()
    await engine.dispose()


async def _new_build(
    conn: AsyncConnection, project: str, *, status: str = "ready", age_days: int = 0
) -> uuid.UUID:
    build_id: uuid.UUID = (
        await conn.execute(
            tables.builds.insert()
            .values(
                project=project,
                status=status,
                started_at=NOW - timedelta(days=age_days),
            )
            .returning(tables.builds.c.id)
        )
    ).scalar_one()
    await conn.commit()
    return build_id


@pytest.mark.integration
async def test_activation_flips_atomically_and_rollback_restores(project: str) -> None:
    """DR-001: exactly one active build ever exists; activation archives the
    old active in the SAME transaction that promotes the new one; rollback is
    activation of the previously-active build. Empty builds — the three
    stores trivially agree (0 == 0), isolating the switch semantics."""
    engine = _engine()
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with engine.connect() as conn, driver.session() as session:
            build_a = await _new_build(conn, project)
            build_b = await _new_build(conn, project)

            report = await activate(conn, qdrant, session, project, build_a)
            assert report.ok and report.deferred  # eval deferral is SURFACED
            statuses = {b.id: b.status for b in await list_builds(conn, project)}
            assert statuses[build_a] == "active"

            report = await activate(conn, qdrant, session, project, build_b)
            assert report.ok
            statuses = {b.id: b.status for b in await list_builds(conn, project)}
            assert statuses == {build_a: "archived", build_b: "active"}  # one active, atomically

            # already-active refusal (idempotence is a REFUSAL, not a no-op)
            report = await activate(conn, qdrant, session, project, build_b)
            assert not report.ok and "already active" in report.failures[0]

            target, report = await rollback(conn, qdrant, session, project)
            assert target == build_a and report.ok
            statuses = {b.id: b.status for b in await list_builds(conn, project)}
            assert statuses == {build_a: "active", build_b: "archived"}

            # a build created DIRECTLY as active (schema-legal: activated_at
            # stays NULL) must still be a rollback target after displacement
            await conn.execute(
                tables.builds.update()
                .where(tables.builds.c.project == project)
                .values(status="archived")
            )
            await conn.commit()  # clear the active slot BEFORE the direct insert
            # started_at must sort NEWEST among archived rows: earlier
            # promotions in this test stamped real-clock activated_at, which
            # is later than the module NOW constant — age_days=-1 keeps the
            # ordering deterministic for the fallback key
            direct = await _new_build(conn, project, status="active", age_days=-1)
            fresh = await _new_build(conn, project)
            report = await activate(conn, qdrant, session, project, fresh)
            assert report.ok  # 'direct' displaced with activated_at still NULL
            target, report = await rollback(conn, qdrant, session, project)
            assert target == direct and report.ok  # NULL-activated_at found
    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()


@pytest.mark.integration
async def test_preflight_refuses_unpromotable_and_drifted_builds(project: str) -> None:
    """§14: a 'building' build cannot be activated; a build whose Postgres
    truth disagrees with the projections (entity in PG, nothing in Neo4j) is
    DRIFTED and refused with the counts named (§19). Nothing changes on
    refusal."""
    engine = _engine()
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with engine.connect() as conn, driver.session() as session:
            building = await _new_build(conn, project, status="building")
            report = await preflight(conn, qdrant, session, project, building)
            assert not report.ok and "status is 'building'" in report.failures[0]

            missing = await preflight(conn, qdrant, session, project, uuid.uuid4())
            assert not missing.ok and "not found" in missing.failures[0]

            drifted = await _new_build(conn, project, status="building")
            writer = await BuildScopedWriter.for_building_build(conn, project, drifted)
            await writer.insert(
                tables.entities,
                id=uuid.uuid4(),
                type="org",
                canonical_name="Acme",
                entity_key=fingerprints.entity_key("org", "Acme"),
                status="active",
                review_status="unreviewed",
                created_by="rule",
                created_at=NOW,
                updated_at=NOW,
            )
            await conn.commit()
            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == drifted).values(status="ready")
            )
            await conn.commit()
            report = await activate(conn, qdrant, session, project, drifted)
            assert not report.ok
            assert any("graph drift" in f for f in report.failures)
            statuses = {b.id: b.status for b in await list_builds(conn, project)}
            assert statuses[drifted] == "ready"  # refusal changed NOTHING
    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()


@pytest.mark.integration
async def test_diff_counts_per_table(project: str) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            empty = await _new_build(conn, project)
            full = await _new_build(conn, project, status="building")
            writer = await BuildScopedWriter.for_building_build(conn, project, full)
            entity_id = uuid.uuid4()
            await writer.insert(
                tables.entities,
                id=entity_id,
                type="org",
                canonical_name="Acme",
                entity_key=fingerprints.entity_key("org", "Acme"),
                status="active",
                review_status="unreviewed",
                created_by="rule",
                created_at=NOW,
                updated_at=NOW,
            )
            await writer.insert_entity_mention(
                entity_id=entity_id,
                source_kind="text",
                source_ref="chunk-1",
                surface_form="Acme",
                confidence=1.0,
            )
            await conn.commit()
            partner_id = uuid.uuid4()
            await writer.insert(
                tables.entities,
                id=partner_id,
                type="org",
                canonical_name="Globex",
                entity_key=fingerprints.entity_key("org", "Globex"),
                status="active",
                review_status="unreviewed",
                created_by="rule",
                created_at=NOW,
                updated_at=NOW,
            )
            await writer.insert(
                tables.merge_candidates,
                id=uuid.uuid4(),
                left_entity_id=entity_id,
                right_entity_id=partner_id,
                score=0.9,
                status="pending",
            )
            await conn.commit()
            table_diff = await diff(conn, project, empty, full)
            assert table_diff["entities"] == {"a": 0, "b": 2, "delta": 2}
            assert table_diff["entity_mentions"] == {"a": 0, "b": 1, "delta": 1}
            # merge-review work is build state too — it must show in the diff
            assert table_diff["merge_candidates"] == {"a": 0, "b": 1, "delta": 1}
            assert table_diff["documents"]["delta"] == 0

            # a foreign build id must be REFUSED, not mixed cross-project
            with pytest.raises(ValueError, match="does not belong to project"):
                await diff(conn, project, empty, uuid.uuid4())
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_prune_keeps_the_window_and_always_the_active(project: str) -> None:
    """§14 GC: newest ``keep`` survive; the ACTIVE build survives regardless
    of age; victims disappear from Postgres including their FK children
    (mentions via the entity FK — no build_id of their own)."""
    engine = _engine()
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with engine.connect() as conn, driver.session() as session:
            oldest = await _new_build(conn, project, age_days=9)
            old_active = await _new_build(conn, project, age_days=8)
            mid = await _new_build(conn, project, age_days=5)
            newest = await _new_build(conn, project, age_days=1)
            report = await activate(conn, qdrant, session, project, old_active)
            assert report.ok

            victims = await prune(conn, qdrant, session, project, keep=2)
            assert set(victims) == {oldest}  # newest 2 kept + active kept

            remaining = {b.id for b in await list_builds(conn, project)}
            assert remaining == {old_active, mid, newest}
            statuses = {b.id: b.status for b in await list_builds(conn, project)}
            assert statuses[old_active] == "active"  # never pruned

            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == mid).values(status="archived")
            )
            await conn.commit()
            victims = await prune(conn, qdrant, session, project, keep=1)
            # keep=1 window = {newest}; active kept by rule; mid archived → swept
            assert set(victims) == {mid}
            remaining = {b.id for b in await list_builds(conn, project)}
            assert remaining == {old_active, newest}
    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()


@pytest.mark.integration
async def test_a_resolved_and_projected_build_passes_preflight(project: str) -> None:
    """The over-block dual (local review blocker): the projections hold only
    the ACTIVE subset of the SoR — merged/rejected rows stay in Postgres by
    design (§17). Drift must compare the PROJECTED populations, not raw row
    counts, or every resolved build is refused. Here: 1 active + 1 merged
    entity, 1 active relation whose dst did not survive resolution (projected
    as a SKIP, §5) — really projected via index_build — must preflight OK."""
    from typing import cast

    from llama_index.core.base.embeddings.base import BaseEmbedding

    from core.index.indexing import index_build
    from core.stores.graph import BuildScopedGraphProjector
    from core.stores.vectors import BuildScopedVectorProjector

    class _Embedder:
        async def aget_text_embedding(self, text: str) -> list[float]:
            return [float(len(text)), 1.0, 0.0, 0.0]

    engine = _engine()
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with engine.connect() as conn, driver.session() as session:
            build_id = await _new_build(conn, project, status="building")
            writer = await BuildScopedWriter.for_building_build(conn, project, build_id)

            async def _entity(name: str, status: str) -> uuid.UUID:
                entity_id = uuid.uuid4()
                await writer.insert(
                    tables.entities,
                    id=entity_id,
                    type="org",
                    canonical_name=name,
                    entity_key=fingerprints.entity_key("org", name),
                    status=status,
                    review_status="unreviewed",
                    created_by="rule",
                    created_at=NOW,
                    updated_at=NOW,
                )
                return entity_id

            survivor = await _entity("Acme", "active")
            partner = await _entity("Globex", "active")
            casualty = await _entity("Acme Corp", "merged")

            async def _relation(
                src: uuid.UUID, dst: uuid.UUID, src_name: str, dst_name: str
            ) -> None:
                await writer.insert(
                    tables.relations,
                    id=uuid.uuid4(),
                    src_entity_id=src,
                    dst_entity_id=dst,
                    type="partners_with",
                    relation_signature=fingerprints.relation_signature(
                        fingerprints.entity_key("org", src_name),
                        "partners_with",
                        fingerprints.entity_key("org", dst_name),
                    ),
                    status="active",
                    review_status="unreviewed",
                    created_by="rule",
                    created_at=NOW,
                    updated_at=NOW,
                )

            # one PROJECTED edge (both endpoints active — would have caught
            # the round-1 P1: an edge-count predicate that no real edge
            # satisfies makes every relation-bearing build "drifted")...
            await _relation(survivor, partner, "Acme", "Globex")
            # ...and one SKIPPED edge (dst did not survive resolution)
            await _relation(survivor, casualty, "Acme", "Acme Corp")
            await conn.commit()

            vectors = await BuildScopedVectorProjector.for_building_build(
                conn, qdrant, project, build_id
            )
            graph = await BuildScopedGraphProjector.for_building_build(
                conn, session, project, build_id
            )
            report = await index_build(writer, cast(BaseEmbedding, _Embedder()), vectors, graph)
            await conn.commit()
            assert report.entities_projected == 2  # the active pair only
            assert report.relations_projected == 1  # the active↔active edge
            assert report.relations_skipped == 1  # dst didn't survive

            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == build_id).values(status="ready")
            )
            await conn.commit()

            check = await preflight(conn, qdrant, session, project, build_id)
            assert check.ok, f"resolved build refused: {check.failures}"

    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()


@pytest.mark.integration
async def test_prune_skips_a_victim_promoted_after_the_snapshot(project: str) -> None:
    """Class 10: the victim set is a bind-time SNAPSHOT — the deleting
    transaction re-checks status under the victim's ROW LOCK. Interleaving
    (two connections, deterministic): conn2 locks the victim FIRST, prune
    blocks on that lock, conn2 promotes the victim to ACTIVE and commits —
    prune's re-check then sees 'active' and SKIPS: data intact, excluded
    from the pruned list. Reverting the FOR-UPDATE+re-check makes prune
    delete the just-activated build → this test fails."""
    import asyncio

    from sqlalchemy import select

    engine, engine2 = _engine(), _engine()
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with (
            engine.connect() as conn,
            engine2.connect() as conn2,
            driver.session() as session,
        ):
            victim = await _new_build(conn, project, age_days=9)
            keeper = await _new_build(conn, project, age_days=1)
            assert keeper is not None

            # conn2 takes the victim's row lock BEFORE prune runs
            txn2 = await conn2.begin()
            await conn2.execute(
                select(tables.builds.c.status).where(tables.builds.c.id == victim).with_for_update()
            )

            prune_task = asyncio.ensure_future(prune(conn, qdrant, session, project, keep=1))
            done, _ = await asyncio.wait([prune_task], timeout=1.0)
            assert not done  # prune is BLOCKED on the held lock, pre-delete

            # promote the victim while prune waits, then release the lock
            await conn2.execute(
                tables.builds.update()
                .where(tables.builds.c.project == project, tables.builds.c.status == "active")
                .values(status="archived")
            )
            await conn2.execute(
                tables.builds.update().where(tables.builds.c.id == victim).values(status="active")
            )
            await txn2.commit()

            pruned = await asyncio.wait_for(prune_task, timeout=10)
            assert victim not in pruned  # the re-check under the lock SKIPPED it
            statuses = {b.id: b.status for b in await list_builds(conn, project)}
            assert statuses[victim] == "active"  # promoted build survives intact
    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()
        await engine2.dispose()
