"""Why: §7's promises are cross-table cascades that only live Postgres can
prove — a merge must survive the unique indexes it dances around (entity_key,
relation signature partial-unique, evidence dedup), carry forward through the
NON-build-scoped ledger into the next build (DR-003), converge on re-run
(§5), and the fenced update/delete/repoint guards must refuse cross-build
rows and post-activation writes (TOCTOU) exactly like insert does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.resolve import fingerprints
from core.resolve.resolution import ResolutionConfig, resolve_build
from core.stores.repo import BuildNotWritableError, BuildScopedWriter, RowNotInBuildError
from core.stores.tables import (
    builds,
    entities,
    merge_candidates,
    relation_evidence,
    relations,
    review_ledger,
)
from tests.conftest import ensure_project

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def _writer(conn: AsyncConnection, project: str) -> BuildScopedWriter:
    await ensure_project(conn, project)
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def _entity(
    writer: BuildScopedWriter, etype: str, name: str, *, mentions: int = 1
) -> tuple[uuid.UUID, str]:
    key = fingerprints.entity_key(etype, name)
    entity_id = uuid.uuid4()
    await writer.insert(
        entities,
        id=entity_id,
        type=etype,
        canonical_name=name,
        entity_key=key,
        status="active",
        review_status="unreviewed",
        created_by="rule",
        created_at=NOW,
        updated_at=NOW,
    )
    for i in range(mentions):
        await writer.insert_entity_mention(
            entity_id=entity_id,
            source_kind="text",
            source_ref=f"chunk:{'0' * 63}{i}:{i}",
            surface_form=name,
            confidence=1.0,
        )
    return entity_id, key


async def _relation(
    writer: BuildScopedWriter,
    src: tuple[uuid.UUID, str],
    rtype: str,
    dst: tuple[uuid.UUID, str],
    *,
    evidence_ref: str,
) -> uuid.UUID:
    sig = fingerprints.relation_signature(src[1], rtype, dst[1])
    relation_id = uuid.uuid4()
    await writer.insert(
        relations,
        id=relation_id,
        src_entity_id=src[0],
        dst_entity_id=dst[0],
        type=rtype,
        relation_signature=sig,
        status="active",
        review_status="unreviewed",
        created_by="rule",
        confidence=1.0,
        created_at=NOW,
        updated_at=NOW,
    )
    await writer.insert(
        relation_evidence,
        id=uuid.uuid4(),
        relation_id=relation_id,
        evidence_type="row",
        evidence_ref=evidence_ref,
        evidence_hash=fingerprints.evidence_hash(sig, evidence_ref, None),
        confidence=1.0,
        created_at=NOW,
    )
    return relation_id


async def test_auto_merge_cascades_and_converges(migrated: None) -> None:
    """The full §7 arc on live PG: near-identical companies auto-merge (the
    busier one canonical), mentions repoint, the loser's relation re-mints
    its signature from the canonical's key with its evidence re-hashed —
    surviving every unique index — and the decision lands in the ledger as
    decided_by='auto'. A second pass changes nothing (§5)."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer = await _writer(conn, project)
            acme = await _entity(writer, "Company", "Acme Corporation", mentions=3)
            acme2 = await _entity(writer, "Company", "Acme Corporatio", mentions=1)  # typo twin
            alice = await _entity(writer, "Person", "Alice", mentions=1)
            await _relation(writer, alice, "WORKS_AT", acme2, evidence_ref="9:employees:7")

            report = await resolve_build(conn, writer, ResolutionConfig())
            assert report.auto_merged == 1
            assert report.mentions_repointed == 1
            assert report.relations_reminted == 1

            loser = (await conn.execute(entities.select().where(entities.c.id == acme2[0]))).one()
            assert loser.status == "merged"
            assert loser.attributes["merged_into"] == str(acme[0])
            # the relation now points at the canonical, re-minted + re-hashed
            edge = (
                await conn.execute(relations.select().where(relations.c.type == "WORKS_AT"))
            ).one()
            expected_sig = fingerprints.relation_signature(alice[1], "WORKS_AT", acme[1])
            assert edge.dst_entity_id == acme[0]
            assert edge.relation_signature == expected_sig
            ev = (
                await conn.execute(
                    relation_evidence.select().where(relation_evidence.c.relation_id == edge.id)
                )
            ).one()
            assert ev.evidence_hash == fingerprints.evidence_hash(
                expected_sig, "9:employees:7", None
            )
            ledger_row = (
                await conn.execute(review_ledger.select().where(review_ledger.c.project == project))
            ).one()
            assert (ledger_row.target_kind, ledger_row.decision, ledger_row.decided_by) == (
                "merge",
                "merge",
                "auto",
            )

            second = await resolve_build(conn, writer, ResolutionConfig())
            assert second == type(second)(
                **dict.fromkeys(second.__dataclass_fields__, 0)
            )  # all-zero: converged
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_ledger_merge_carries_into_the_next_build(migrated: None) -> None:
    """DR-003: build #2 re-extracts the same pair; the ledger's merge applies
    REGARDLESS of score (thresholds set impossibly high to prove the path),
    and a manual reject on another pair suppresses even a perfect score."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer1 = await _writer(conn, project)
            await _entity(writer1, "Company", "Acme Corporation", mentions=2)
            await _entity(writer1, "Company", "Acme Corporatio", mentions=1)
            await resolve_build(conn, writer1, ResolutionConfig())  # mints the auto decision

            # a curator rejects merging two branches that LOOK identical
            left = fingerprints.ledger_entity_key("Initech Ltd")
            right = fingerprints.ledger_entity_key("Initech Ltd.")
            await conn.execute(
                review_ledger.insert().values(
                    project=project,
                    target_kind="merge",
                    target_key=fingerprints.ledger_merge_key(left, right),
                    fingerprint_version=fingerprints.LEDGER_FINGERPRINT_VERSION,
                    decision="reject",
                    decided_by="curator-1",
                    decided_at=NOW,
                    reason="separate legal entities",
                )
            )

            writer2 = await _writer(conn, project)
            await _entity(writer2, "Company", "Acme Corporation", mentions=2)
            await _entity(writer2, "Company", "Acme Corporatio", mentions=1)
            await _entity(writer2, "Company", "Initech Ltd", mentions=1)
            await _entity(writer2, "Company", "Initech Ltd.", mentions=1)

            strict = ResolutionConfig(auto_merge_threshold=1.0, review_threshold=1.0)
            report = await resolve_build(conn, writer2, strict)
            assert report.ledger_merged == 1  # Acme pair merged by carried decision
            assert report.auto_merged == 0
            assert report.pairs_suppressed == 1  # Initech pair silenced by the reject
            assert report.candidates_created == 0
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_mid_score_creates_one_pending_candidate(migrated: None) -> None:
    """The review band: a plausible-but-unsure pair lands ONE pending
    merge_candidates row with score/snapshots/impact, and a re-run does not
    duplicate it."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer = await _writer(conn, project)
            await _entity(writer, "Company", "Acme Corporation", mentions=2)
            await _entity(writer, "Company", "Acme Corporation Ltd", mentions=1)  # ratio ~0.89
            config = ResolutionConfig(auto_merge_threshold=0.99, review_threshold=0.5)
            report = await resolve_build(conn, writer, config)
            assert report.auto_merged == 0 and report.candidates_created == 1
            row = (
                await conn.execute(
                    merge_candidates.select().where(merge_candidates.c.project == project)
                )
            ).one()
            assert row.status == "pending" and 0.5 <= row.score < 0.99
            assert row.left_snapshot["entity_key"] and row.right_snapshot["entity_key"]
            # sides follow deterministic entity_key processing order, so
            # assert the pair's mention counts without assuming which side
            assert {row.impact["left_mentions"], row.impact["right_mentions"]} == {1, 2}

            again = await resolve_build(conn, writer, config)
            assert again.candidates_created == 0
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_curator_merge_survives_type_drift_across_builds(migrated: None) -> None:
    """DR-011's 白審 kill, end-to-end on live PG across the TWO code paths:
    ``decide_merge_candidate`` (the curator write) and ``resolve_build`` (the
    carry-forward read) must mint the SAME type-free v2 ledger key — build #1
    records the merge over an Exhibit-typed pair, build #2's LLM re-types
    BOTH sides (the 全量實測 drift: EXHIBIT→LOCATION/FACILITY), and the
    decision still carries instead of re-surfacing for 白審. Under the v1
    type-bearing keys this exact flow lost 1/3 of the full-run decisions."""
    from core.resolve.decisions import decide_merge_candidate

    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            # build #1: the twin typed Exhibit; a pending candidate a curator merges
            writer1 = await _writer(conn, project)
            left_id, _ = await _entity(writer1, "Exhibit", "區域探索廳", mentions=2)
            right_id, _ = await _entity(writer1, "Exhibit", "區域探索厅", mentions=1)
            candidate_id = uuid.uuid4()
            await writer1.insert(
                merge_candidates,
                id=candidate_id,
                left_entity_id=left_id,
                right_entity_id=right_id,
                score=0.9,
                status="pending",
            )
            await decide_merge_candidate(
                conn,
                project=project,
                build_id=writer1.build_id,
                candidate_id=candidate_id,
                verb="approve",
                decided_by="curator-1",
                reason="同一個展廳",
            )

            # build #2: the LLM re-typed BOTH sides — the decision must carry
            writer2 = await _writer(conn, project)
            await _entity(writer2, "Location", "區域探索廳", mentions=2)
            await _entity(writer2, "Facility", "區域探索厅", mentions=1)
            strict = ResolutionConfig(auto_merge_threshold=1.0, review_threshold=1.0)
            report = await resolve_build(conn, writer2, strict)
            assert report.ledger_merged == 1  # carried across the drift — no 白審
            assert report.auto_merged == 0
            assert report.candidates_created == 0  # never re-surfaced
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_entity_reject_excludes_from_projection_and_pairing(migrated: None) -> None:
    """§17: a ledger reject on an entity_key marks the row rejected (C5
    filters on status) and keeps it out of scoring entirely."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer = await _writer(conn, project)
            await _entity(writer, "Company", "Acme Corporation")
            bad = await _entity(writer, "Company", "Acme Corporatio")
            await conn.execute(
                review_ledger.insert().values(
                    project=project,
                    target_kind="entity",
                    # the TYPE-FREE v2 entity ledger key (DR-011), not the
                    # stored type-bearing entity_key
                    target_key=fingerprints.ledger_entity_key("Acme Corporatio"),
                    fingerprint_version=fingerprints.LEDGER_FINGERPRINT_VERSION,
                    decision="reject",
                    decided_by="curator-1",
                    decided_at=NOW,
                    reason="hallucination",
                )
            )
            report = await resolve_build(conn, writer, ResolutionConfig())
            assert report.entities_rejected == 1
            assert report.auto_merged == 0  # the rejected twin never paired
            row = (await conn.execute(entities.select().where(entities.c.id == bad[0]))).one()
            assert (row.status, row.review_status) == ("rejected", "rejected")
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_remint_collision_demotes_duplicate_and_dedups_evidence(migrated: None) -> None:
    """The C3a-flagged coupling's hardest corner: canonical→X already exists
    AND loser→X exists. The merge demotes the loser's edge (status='merged',
    signature freed to NULL, audit trail in attributes) and moves its
    evidence — deleting any piece whose re-hash collides with a stored twin
    (§27.4 dedup), keeping distinct provenance."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer = await _writer(conn, project)
            acme = await _entity(writer, "Company", "Acme Corporation", mentions=3)
            twin = await _entity(writer, "Company", "Acme Corporatio", mentions=1)
            alice = await _entity(writer, "Person", "Alice", mentions=1)
            await _relation(writer, alice, "WORKS_AT", acme, evidence_ref="9:employees:7")
            # same fact via the twin: one duplicate evidence, one distinct
            dup = await _relation(writer, alice, "WORKS_AT", twin, evidence_ref="9:employees:7")
            await writer.insert(
                relation_evidence,
                id=uuid.uuid4(),
                relation_id=dup,
                evidence_type="row",
                evidence_ref="9:employees:8",
                evidence_hash=fingerprints.evidence_hash(
                    fingerprints.relation_signature(alice[1], "WORKS_AT", twin[1]),
                    "9:employees:8",
                    None,
                ),
                confidence=1.0,
                created_at=NOW,
            )

            report = await resolve_build(conn, writer, ResolutionConfig())
            assert report.duplicate_edges_demoted == 1
            assert report.duplicate_evidence_deleted == 1

            demoted = (await conn.execute(relations.select().where(relations.c.id == dup))).one()
            assert demoted.status == "merged" and demoted.relation_signature is None
            assert demoted.attributes["former_signature"]
            survivor_sig = fingerprints.relation_signature(alice[1], "WORKS_AT", acme[1])
            survivor = (
                await conn.execute(
                    relations.select().where(relations.c.relation_signature == survivor_sig)
                )
            ).one()
            ev_rows = (
                await conn.execute(
                    relation_evidence.select().where(relation_evidence.c.relation_id == survivor.id)
                )
            ).fetchall()
            # the duplicate collapsed; the distinct piece moved over, re-hashed
            assert {e.evidence_ref for e in ev_rows} == {"9:employees:7", "9:employees:8"}
            assert all(
                e.evidence_hash == fingerprints.evidence_hash(survivor_sig, e.evidence_ref, e.quote)
                for e in ev_rows
            )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_mutation_guards_refuse_cross_scope_and_post_activation(migrated: None) -> None:
    """The new fenced surface behaves like insert: another build's row is a
    typed refusal (not a silent no-op), scope columns are unupdatable, and a
    build that activates mid-flight refuses further mutation (TOCTOU)."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            writer_a = await _writer(conn, project)
            foreign = await _entity(writer_a, "Company", "Acme")
            writer_b = await _writer(conn, project)
            with pytest.raises(RowNotInBuildError):
                await writer_b.update(entities, foreign[0], status="merged")
            with pytest.raises(ValueError, match="scope columns"):
                await writer_a.update(entities, foreign[0], build_id=uuid.uuid4())

            await conn.execute(
                builds.update().where(builds.c.id == writer_a.build_id).values(status="active")
            )
            with pytest.raises(BuildNotWritableError):
                await writer_a.update(entities, foreign[0], status="merged", updated_at=NOW)
            with pytest.raises(BuildNotWritableError):
                await writer_a.repoint_mentions(foreign[0], foreign[0])
            await trans.rollback()
    finally:
        await engine.dispose()
