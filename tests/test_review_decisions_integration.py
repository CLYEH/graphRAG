"""Why: a review decision is the Console's ONLY write into the resolution
world, and everything §17/§27.3 promises hangs on its transaction shape —
the ledger entry must carry the SAME merge_key resolve will read next build
(else the decision silently never applies), defer must land in the ledger at
all (#28 R4a: a deferred pair must block auto-merge), terminal states must
refuse re-decision UNDER the row lock (two racing curators converge to one
decision), and the audit stamps must be one instant. Fakes can't prove the
SQL; rolled-back transactions keep the dev DB clean (committed fixtures only
for the race, with cleanup).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.registry import create_project
from core.resolve import fingerprints
from core.resolve.decisions import (
    EntityNotFoundError,
    InvalidReviewTransitionError,
    MergeCandidateNotFoundError,
    RelationNotFoundError,
    decide_entity,
    decide_merge_candidate,
    decide_relation,
)
from core.stores.tables import (
    builds,
    entities,
    merge_candidates,
    projects,
    relations,
    review_ledger,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _proj() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


async def _seed_candidate(
    conn: AsyncConnection, project: str
) -> tuple[uuid.UUID, uuid.UUID, str, str]:
    """Project + active build + two entities + one pending candidate.
    Returns (build_id, candidate_id, left_key, right_key)."""
    await create_project(conn, name=project)
    build_id = (
        await conn.execute(
            builds.insert().values(project=project, status="active").returning(builds.c.id)
        )
    ).scalar_one()
    keys = []
    ids = []
    for name in ("Alice", "Alyce"):
        key = f"fpv1:person|{name.lower()}-{uuid.uuid4().hex[:6]}"
        keys.append(key)
        ids.append(
            (
                await conn.execute(
                    entities.insert()
                    .values(
                        project=project,
                        build_id=build_id,
                        type="Person",
                        canonical_name=name,
                        entity_key=key,
                        status="active",
                    )
                    .returning(entities.c.id)
                )
            ).scalar_one()
        )
    candidate_id = (
        await conn.execute(
            merge_candidates.insert()
            .values(
                project=project,
                build_id=build_id,
                left_entity_id=ids[0],
                right_entity_id=ids[1],
                score=0.85,
            )
            .returning(merge_candidates.c.id)
        )
    ).scalar_one()
    return build_id, candidate_id, keys[0], keys[1]


async def test_decision_writes_ledger_and_audit_in_lockstep(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            build_id, candidate_id, _, _ = await _seed_candidate(conn, project)

            decided = await decide_merge_candidate(
                conn,
                project=project,
                build_id=build_id,
                candidate_id=candidate_id,
                verb="approve",
                decided_by="console",
                reason="same person, spelling variant",
            )
            assert decided.status == "approved" and decided.decision == "approve"
            assert decided.decided_by == "console"
            assert decided.reason == "same person, spelling variant"

            ledger = (
                await conn.execute(
                    sa.select(review_ledger).where(review_ledger.c.project == project)
                )
            ).one()
            # the carry-forward key is EXACTLY what resolve will recompute —
            # the TYPE-FREE v2 ledger_merge_key over (name, disambiguator)
            # pairs (DR-011): the decision must survive either side being
            # re-typed by the next build's extraction
            assert ledger.target_key == fingerprints.ledger_merge_key(
                fingerprints.ledger_entity_key("Alice"),
                fingerprints.ledger_entity_key("Alyce"),
            )
            assert ledger.target_kind == "merge"
            assert ledger.fingerprint_version == fingerprints.LEDGER_FINGERPRINT_VERSION
            assert ledger.decision == "approve" and ledger.decided_by == "console"
            assert ledger.reason == "same person, spelling variant"
            # one instant: now() is transaction-stable, both stamps identical
            assert ledger.decided_at == decided.decided_at
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_defer_writes_a_ledger_entry_and_stays_decidable(migrated: None) -> None:
    # WHY (#28 R4a): a deferred pair must BLOCK auto-merge, so resolve has to
    # SEE the defer in the ledger — and §17 keeps deferred decidable
    # (deferred → approved|rejected), producing a SECOND entry whose later
    # decided_at wins §27.3 precedence.
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            build_id, candidate_id, _, _ = await _seed_candidate(conn, project)

            deferred = await decide_merge_candidate(
                conn,
                project=project,
                build_id=build_id,
                candidate_id=candidate_id,
                verb="defer",
                decided_by="console",
            )
            assert deferred.status == "deferred"
            approved = await decide_merge_candidate(
                conn,
                project=project,
                build_id=build_id,
                candidate_id=candidate_id,
                verb="approve",
                decided_by="console",
            )
            assert approved.status == "approved"
            rows = (
                await conn.execute(
                    sa.select(review_ledger.c.decision, review_ledger.c.decided_at).where(
                        review_ledger.c.project == project
                    )
                )
            ).all()
            assert sorted(r.decision for r in rows) == ["approve", "defer"]
            # WHY (Codex #59 R2): both decisions ran in THIS one transaction —
            # txn-stable now() would tie them and §27.3's latest-wins
            # precedence would pick arbitrarily; per-decision clock_timestamp
            # keeps them ordered so effective_decision resolves to the approve
            by_verb = {r.decision: r.decided_at for r in rows}
            assert by_verb["approve"] > by_verb["defer"]
            from core.resolve.review import LedgerEntry, effective_decision

            winner = effective_decision(
                [
                    LedgerEntry(decision=r.decision, decided_by="console", decided_at=r.decided_at)
                    for r in rows
                ]
            )
            assert winner is not None and winner.decision == "approve"
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_terminal_states_refuse_and_unknowns_are_typed(migrated: None) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            project = _proj()
            build_id, candidate_id, _, _ = await _seed_candidate(conn, project)
            await decide_merge_candidate(
                conn,
                project=project,
                build_id=build_id,
                candidate_id=candidate_id,
                verb="reject",
                decided_by="console",
            )
            with pytest.raises(InvalidReviewTransitionError) as ei:
                await decide_merge_candidate(
                    conn,
                    project=project,
                    build_id=build_id,
                    candidate_id=candidate_id,
                    verb="approve",
                    decided_by="console",
                )
            assert ei.value.current == "rejected" and ei.value.verb == "approve"

            with pytest.raises(MergeCandidateNotFoundError):
                await decide_merge_candidate(
                    conn,
                    project=project,
                    build_id=build_id,
                    candidate_id=uuid.uuid4(),
                    verb="approve",
                    decided_by="console",
                )
            with pytest.raises(ValueError, match="unknown decision verb"):
                await decide_merge_candidate(
                    conn,
                    project=project,
                    build_id=build_id,
                    candidate_id=candidate_id,
                    verb="merge",  # a LEDGER vocab word, not a contract verb
                    decided_by="console",
                )
            for illegal in ("auto", ""):  # §27.3 impersonation + the empty cell
                with pytest.raises(ValueError, match="reserved/empty"):
                    await decide_merge_candidate(
                        conn,
                        project=project,
                        build_id=build_id,
                        candidate_id=candidate_id,
                        verb="approve",
                        decided_by=illegal,
                    )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_racing_decides_converge_to_one_decision(migrated: None) -> None:
    # WHY (class 10): the §17 check lives UNDER the FOR UPDATE lock — two
    # concurrent curators serialize; the loser re-reads the winner's terminal
    # status and gets the typed refusal, never a double ledger entry pair of
    # conflicting verbs racing each other.
    engine = _engine()
    project = _proj()
    try:
        async with engine.connect() as seed, seed.begin():
            build_id, candidate_id, _, _ = await _seed_candidate(seed, project)

        async with engine.connect() as conn_a, engine.connect() as conn_b:
            txn_a = await conn_a.begin()
            await decide_merge_candidate(
                conn_a,
                project=project,
                build_id=build_id,
                candidate_id=candidate_id,
                verb="approve",
                decided_by="curator-a",
            )  # holds the row lock, uncommitted

            async def _contender() -> None:
                async with conn_b.begin():
                    await decide_merge_candidate(
                        conn_b,
                        project=project,
                        build_id=build_id,
                        candidate_id=candidate_id,
                        verb="reject",
                        decided_by="curator-b",
                    )

            contender = asyncio.create_task(_contender())
            await asyncio.sleep(0.3)
            assert not contender.done()  # blocked on the lock, not double-decided
            await txn_a.commit()
            with pytest.raises(InvalidReviewTransitionError) as ei:
                await contender
            assert ei.value.current == "approved"

        async with engine.connect() as check:
            decisions = (
                await check.execute(
                    sa.select(review_ledger.c.decision).where(review_ledger.c.project == project)
                )
            ).scalars()
            assert list(decisions) == ["approve"]  # exactly one entry, the winner's
    finally:
        async with engine.connect() as cleanup:
            await cleanup.execute(review_ledger.delete().where(review_ledger.c.project == project))
            await cleanup.execute(
                merge_candidates.delete().where(merge_candidates.c.project == project)
            )
            await cleanup.execute(entities.delete().where(entities.c.project == project))
            await cleanup.execute(builds.delete().where(builds.c.project == project))
            await cleanup.execute(projects.delete().where(projects.c.name == project))
            await cleanup.commit()
        await engine.dispose()


async def _seed_entity(
    conn: AsyncConnection, project: str, build_id: uuid.UUID, name: str, **over: object
) -> uuid.UUID:
    values: dict[str, object] = {
        "project": project,
        "build_id": build_id,
        "type": "Person",
        "canonical_name": name,
        "entity_key": f"fpv1:person|{name.lower()}-{uuid.uuid4().hex[:6]}",
        "status": "needs_review",
        "review_status": "unreviewed",
    }
    values.update(over)
    return cast(
        "uuid.UUID",
        (
            await conn.execute(entities.insert().values(**values).returning(entities.c.id))
        ).scalar_one(),
    )


async def test_decide_entity_writes_the_carryforward_key_and_updates_the_row(
    migrated: None,
) -> None:
    """GOV2: an entity approve writes a review_ledger entry keyed by the EXACT
    type-free v2 ledger_entity_key resolve re-mints (so it carries forward), and
    updates the ACTIVE build's row — approve on a needs_review entity RESTORES
    status=active + review_status=approved (mirrors resolution's application)."""
    engine = _engine()
    project = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=project)
            build_id = (
                await conn.execute(
                    builds.insert().values(project=project, status="active").returning(builds.c.id)
                )
            ).scalar_one()
            eid = await _seed_entity(conn, project, build_id, "區域探索廳", disambiguator="EXT-42")

            updated = await decide_entity(
                conn,
                project=project,
                build_id=build_id,
                entity_id=eid,
                verb="approve",
                decided_by="console",
                reason="valid exhibit",
            )
            # the row: needs_review → active, review_status → approved
            assert updated.status == "active" and updated.review_status == "approved"

            ledger = (
                await conn.execute(
                    sa.select(review_ledger).where(review_ledger.c.project == project)
                )
            ).one()
            assert ledger.target_kind == "entity" and ledger.decision == "approve"
            assert ledger.decided_by == "console" and ledger.reason == "valid exhibit"
            assert ledger.fingerprint_version == fingerprints.LEDGER_FINGERPRINT_VERSION
            # the KEY equals what resolve_build re-mints from (name, disambiguator)
            assert ledger.target_key == fingerprints.ledger_entity_key("區域探索廳", "EXT-42")

            # §17: an approved entity is terminal — re-deciding refuses
            with pytest.raises(InvalidReviewTransitionError):
                await decide_entity(
                    conn,
                    project=project,
                    build_id=build_id,
                    entity_id=eid,
                    verb="reject",
                    decided_by="console",
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_decide_entity_reject_excludes_and_scopes_by_build(migrated: None) -> None:
    """reject sets status=rejected + review_status=rejected (the C5 index filters
    on status, so the row leaves the projection). An entity_id under a DIFFERENT
    build is a not-found — never a cross-build decision."""
    engine = _engine()
    project = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=project)
            active = (
                await conn.execute(
                    builds.insert().values(project=project, status="active").returning(builds.c.id)
                )
            ).scalar_one()
            other = (
                await conn.execute(
                    builds.insert()
                    .values(project=project, status="archived")
                    .returning(builds.c.id)
                )
            ).scalar_one()
            eid = await _seed_entity(conn, project, active, "Ghost")

            with pytest.raises(EntityNotFoundError):  # right id, wrong build
                await decide_entity(
                    conn,
                    project=project,
                    build_id=other,
                    entity_id=eid,
                    verb="reject",
                    decided_by="console",
                )

            rejected = await decide_entity(
                conn,
                project=project,
                build_id=active,
                entity_id=eid,
                verb="reject",
                decided_by="console",
            )
            assert rejected.status == "rejected" and rejected.review_status == "rejected"
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_decide_relation_keys_on_endpoint_ledger_keys(migrated: None) -> None:
    """GOV2: a relation decision keys on ledger_relation_signature over the
    ENDPOINTS' ledger keys (not the row's relation_signature), the EXACT key
    resolve re-mints — so it survives an endpoint being re-typed. Curator
    decided_by must not impersonate the pipeline."""
    engine = _engine()
    project = _proj()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(conn, name=project)
            build_id = (
                await conn.execute(
                    builds.insert().values(project=project, status="active").returning(builds.c.id)
                )
            ).scalar_one()
            src = await _seed_entity(conn, project, build_id, "Alice", disambiguator="A1")
            dst = await _seed_entity(conn, project, build_id, "Acme")
            rid = (
                await conn.execute(
                    relations.insert()
                    .values(
                        project=project,
                        build_id=build_id,
                        src_entity_id=src,
                        dst_entity_id=dst,
                        type="WORKS_AT",
                        relation_signature="fpv1:alice|works_at|acme",
                        status="needs_review",
                        review_status="unreviewed",
                    )
                    .returning(relations.c.id)
                )
            ).scalar_one()

            updated = await decide_relation(
                conn,
                project=project,
                build_id=build_id,
                relation_id=rid,
                verb="approve",
                decided_by="console",
            )
            assert updated.status == "active" and updated.review_status == "approved"

            ledger = (
                await conn.execute(
                    sa.select(review_ledger).where(
                        review_ledger.c.project == project,
                        review_ledger.c.target_kind == "relation",
                    )
                )
            ).one()
            expected = fingerprints.ledger_relation_signature(
                fingerprints.ledger_entity_key("Alice", "A1"),
                "WORKS_AT",
                fingerprints.ledger_entity_key("Acme"),
            )
            assert ledger.target_key == expected

            # a curator must never write decided_by="auto" (§27.3 precedence)
            with pytest.raises(ValueError):
                await decide_relation(
                    conn,
                    project=project,
                    build_id=build_id,
                    relation_id=rid,
                    verb="approve",
                    decided_by="auto",
                )
            with pytest.raises(RelationNotFoundError):
                await decide_relation(
                    conn,
                    project=project,
                    build_id=build_id,
                    relation_id=uuid.uuid4(),
                    verb="approve",
                    decided_by="console",
                )
            await trans.rollback()
    finally:
        await engine.dispose()
