"""Why: the pool's promises are DATABASE behaviors — the upsert must preserve
an existing status on live Postgres (a rejected type re-proposed by the next
build must NOT re-open review: that is DR-003's intent for this artifact),
the auto policy must satisfy the decision-fields-iff-decided CHECK, and the
whole §6 arc must work end-to-end: C3b's held-out TypeProposal lands in the
pool exactly once however many times extraction re-runs.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.graph.documents import TypeProposal
from core.graph.proposals import (
    InvalidProposalTransitionError,
    OntologyProposalNotFoundError,
    decide_ontology_proposal,
    list_ontology_proposals,
    persist_proposals,
)
from core.registry import ProjectNotFoundError, create_project, get_project
from core.resolve.fingerprints import proposal_key
from core.stores.tables import ontology_proposals

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


def _p(kind: str = "entity", name: str = "Spaceship") -> TypeProposal:
    return TypeProposal(kind, name, "Rocinante", "chunk:abc:0")


async def test_pool_upserts_and_rejection_survives_rebuilds(migrated: None) -> None:
    """The load-bearing carry-forward: re-proposing an already-rejected type
    (any casing) is a no-op — the rejection stands, review never re-opens."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            first = await persist_proposals(conn, project, [_p(), _p()])  # in-batch dup
            assert (first.pooled, first.already_present) == (1, 0)

            # a curator rejects it
            await conn.execute(
                ontology_proposals.update()
                .where(ontology_proposals.c.project == project)
                .values(
                    status="rejected",
                    decided_by="curator-1",
                    decided_at=sa.func.now(),
                    reason="not in scope",
                )
            )

            # the "next build" re-proposes the same type, differently spelled
            second = await persist_proposals(conn, project, [_p(name="  spaceship ")])
            assert (second.pooled, second.already_present) == (0, 1)
            row = (
                await conn.execute(
                    ontology_proposals.select().where(ontology_proposals.c.project == project)
                )
            ).one()
            assert row.status == "rejected" and row.decided_by == "curator-1"
            assert row.proposal_key == proposal_key("entity", "Spaceship")
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_auto_policy_lands_accepted_and_satisfies_the_iff_check(migrated: None) -> None:
    """🔧 auto: adoption without review — decided fields present (the CHECK
    demands who/when on any non-proposed row), decider is the policy marker."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            report = await persist_proposals(conn, project, [_p()], policy="auto")
            assert report.pooled == 1
            row = (
                await conn.execute(
                    ontology_proposals.select().where(ontology_proposals.c.project == project)
                )
            ).one()
            assert row.status == "accepted"
            assert row.decided_by == "auto-policy" and row.decided_at is not None
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_decision_fields_iff_decided_is_a_db_invariant(migrated: None) -> None:
    """ALL FOUR bad corners on live PG (a half-tested 'both directions' claim
    is a false-green — the weak `=` form of this CHECK accepted two of them):
    decided+both-null, decided+ANONYMOUS (at set, by null), decided+TIMELESS
    (by set, at null), and proposed+residue are each refused."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    bad_updates: list[dict[str, object]] = [
        {"status": "accepted"},  # decided, both null
        {"status": "accepted", "decided_at": sa.func.now()},  # anonymous decision
        {"status": "accepted", "decided_by": "curator-1"},  # timeless decision
        {"decided_by": "ghost"},  # residue on a proposed row
    ]
    try:
        async with engine.connect() as conn:
            for values in bad_updates:
                trans = await conn.begin()
                await persist_proposals(conn, project, [_p()])
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        ontology_proposals.update()
                        .where(ontology_proposals.c.project == project)
                        .values(**values)
                    )
                await trans.rollback()
    finally:
        await engine.dispose()


async def test_projects_pools_are_independent(migrated: None) -> None:
    """The identity is (project, proposal_key): the same type proposed in two
    projects is two independent pool rows — one project's rejection must not
    silence another's review."""
    engine = _engine()
    a, b = (f"itest-{uuid.uuid4().hex[:10]}" for _ in range(2))
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            assert (await persist_proposals(conn, a, [_p()])).pooled == 1
            assert (await persist_proposals(conn, b, [_p()])).pooled == 1
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_accept_adds_the_type_to_the_configured_ontology(migrated: None) -> None:
    """GOV3's load-bearing behavior: accepting a proposal joins the type to the
    project's CONFIGURED ontology (the same projects.config the extractor reads
    next build), so the next build stops holding it out. Reject leaves config
    untouched; a decided proposal is terminal (§17)."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            await create_project(
                conn,
                name=project,
                config={"ontology": {"entity_types": ["Person"], "relation_types": ["WORKS_AT"]}},
            )
            await persist_proposals(
                conn, project, [TypeProposal("entity", "Exhibit", "區域探索廳", "chunk:a:0")]
            )
            (proposed,) = (await list_ontology_proposals(conn, project, limit=10))[0]

            accepted = await decide_ontology_proposal(
                conn,
                project=project,
                proposal_id=proposed.id,
                verb="accept",
                decided_by="console",
            )
            assert accepted.status == "accepted"
            assert accepted.decided_by == "console" and accepted.decided_at is not None
            # the type joined the CONFIGURED ontology (extractor reads this next build)
            proj = await get_project(conn, project)
            assert proj is not None
            assert "Exhibit" in proj.config["ontology"]["entity_types"]
            assert proj.config["ontology"]["relation_types"] == ["WORKS_AT"]  # untouched

            # §17: an accepted proposal is terminal — re-deciding refuses
            with pytest.raises(InvalidProposalTransitionError):
                await decide_ontology_proposal(
                    conn,
                    project=project,
                    proposal_id=proposed.id,
                    verb="reject",
                    decided_by="console",
                )
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_reject_leaves_config_untouched_and_scopes_by_project(migrated: None) -> None:
    """Reject flips status only — the configured ontology is unchanged (a
    rejected type must NOT enter the vocabulary). And a proposal_id under a
    DIFFERENT project is a not-found (scoped by (project, id): never a
    cross-project decision)."""
    engine = _engine()
    project, other = (f"itest-{uuid.uuid4().hex[:10]}" for _ in range(2))
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            cfg = {"ontology": {"entity_types": ["Person"], "relation_types": ["WORKS_AT"]}}
            await create_project(conn, name=project, config=dict(cfg))
            await create_project(conn, name=other, config=dict(cfg))
            await persist_proposals(
                conn, project, [TypeProposal("relation", "EXHIBITS", "展出", "chunk:a:0")]
            )
            (proposed,) = (await list_ontology_proposals(conn, project, limit=10))[0]

            # the proposal exists, but under `project` — deciding it under `other`
            # is a not-found, never a cross-project write
            with pytest.raises(OntologyProposalNotFoundError):
                await decide_ontology_proposal(
                    conn,
                    project=other,
                    proposal_id=proposed.id,
                    verb="reject",
                    decided_by="console",
                )
            # deciding under a project that does not exist at all
            with pytest.raises(ProjectNotFoundError):
                await decide_ontology_proposal(
                    conn,
                    project=f"ghost-{uuid.uuid4().hex[:6]}",
                    proposal_id=proposed.id,
                    verb="reject",
                    decided_by="console",
                )

            rejected = await decide_ontology_proposal(
                conn, project=project, proposal_id=proposed.id, verb="reject", decided_by="console"
            )
            assert rejected.status == "rejected"
            proj = await get_project(conn, project)
            assert proj is not None
            # relation_types unchanged — a rejected type never enters the vocabulary
            assert proj.config["ontology"]["relation_types"] == ["WORKS_AT"]
            await trans.rollback()
    finally:
        await engine.dispose()


async def test_list_default_queue_vs_status_facet(migrated: None) -> None:
    """The default list is the review queue (proposed only); a decided row
    becomes listable only when the consumer names its status (filter[status])
    — the audit surface, same discipline as merge-candidates."""
    engine = _engine()
    project = f"itest-{uuid.uuid4().hex[:10]}"
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            cfg = {"ontology": {"entity_types": ["Person"], "relation_types": ["WORKS_AT"]}}
            await create_project(conn, name=project, config=dict(cfg))
            await persist_proposals(
                conn,
                project,
                [
                    TypeProposal("entity", "Exhibit", "區域", "chunk:a:0"),
                    TypeProposal("entity", "Vessel", "船", "chunk:b:0"),
                ],
            )
            (both, _) = await list_ontology_proposals(conn, project, limit=10)
            assert len(both) == 2  # both proposed → both in the default queue
            await decide_ontology_proposal(
                conn, project=project, proposal_id=both[0].id, verb="accept", decided_by="console"
            )
            # default queue now shows only the still-proposed one
            (queue, _) = await list_ontology_proposals(conn, project, limit=10)
            assert {p.id for p in queue} == {both[1].id}
            # the accepted one is the audit surface via the status facet
            (accepted, _) = await list_ontology_proposals(
                conn, project, limit=10, status="accepted"
            )
            assert {p.id for p in accepted} == {both[0].id}
            await trans.rollback()
    finally:
        await engine.dispose()
