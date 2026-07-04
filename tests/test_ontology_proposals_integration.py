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
from core.graph.proposals import persist_proposals
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
