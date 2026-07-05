"""Why: global_summary is only correct on the real store — the build-scoped
read (DR-006), the citeable-members CHECK, and cross-build isolation must
hold against live Postgres: an archived build's reports coexist in the table
and must never leak into the active build's response.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.query.global_reports import global_summary
from core.resolve import fingerprints
from core.stores.repo import BuildScopedRepo, BuildScopedWriter
from core.stores.tables import builds, community_reports, entities

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

_SCHEMA = json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def conn(migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = _engine()
    async with engine.connect() as connection:
        yield connection
    await engine.dispose()


async def _new_build(conn: AsyncConnection, project: str) -> BuildScopedWriter:
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def _entity(writer: BuildScopedWriter, name: str) -> uuid.UUID:
    from datetime import UTC, datetime

    entity_id = uuid.uuid4()
    await writer.insert(
        entities,
        id=entity_id,
        type="org",
        canonical_name=name,
        entity_key=fingerprints.entity_key("org", name),
        status="active",
        review_status="unreviewed",
        created_by="rule",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    return entity_id


async def _report(
    writer: BuildScopedWriter,
    title: str,
    rating: float | None,
    member_ids: list[uuid.UUID] | None = None,
) -> None:
    if member_ids is None:
        # members are REAL entities of this build — the grounding check
        # verifies exactly that, so the fixture must satisfy it
        member_ids = [
            await _entity(writer, f"{title}-m1"),
            await _entity(writer, f"{title}-m2"),
        ]
    await writer.insert(
        community_reports,
        id=uuid.uuid4(),
        level=0,
        title=title,
        summary=f"summary of {title}",
        member_entity_ids=member_ids,
        rating=rating,
    )


async def _cleanup(project: str) -> None:
    engine = _engine()
    async with engine.connect() as connection:
        await connection.execute(
            community_reports.delete().where(community_reports.c.project == project)
        )
        await connection.execute(entities.delete().where(entities.c.project == project))
        await connection.execute(builds.delete().where(builds.c.project == project))
        await connection.commit()
    await engine.dispose()


async def test_global_reads_only_the_active_builds_reports(conn: AsyncConnection) -> None:
    project = f"globtest-{uuid.uuid4().hex[:10]}"
    try:
        old = await _new_build(conn, project)
        await _report(old, "stale report", 9.9)  # higher-rated, but archived
        await conn.commit()
        await conn.execute(
            builds.update().where(builds.c.id == old.build_id).values(status="archived")
        )

        new = await _new_build(conn, project)
        await _report(new, "fresh low", 1.0)
        await _report(new, "fresh high", 8.0)
        await conn.commit()
        await conn.execute(
            builds.update().where(builds.c.id == new.build_id).values(status="active")
        )
        await conn.commit()

        repo = await BuildScopedRepo.for_active_build(conn, project)
        response = await global_summary(repo, "what is this corpus about", 10)
        _VALIDATOR.validate(response.to_dict())
        titles = [r.title for r in response.results]
        assert titles == ["fresh high", "fresh low"]  # rating desc, archived NEVER leaks
        assert response.warnings == ()
        for result in response.results:
            assert result.result_type == "community_report"
            assert len(result.source_refs) == 2  # §27.2 member refs, from the live rows
            assert all(ref.source_type == "entity" for ref in result.source_refs)
    finally:
        await _cleanup(project)


async def test_an_ungrounded_member_id_never_becomes_a_live_citation(
    conn: AsyncConnection,
) -> None:
    """member_entity_ids has no FK — a hand-written row can claim ANY uuid.
    On the live store the ungrounded id must be dropped from the refs (and a
    fully-orphaned report dropped whole), never emitted as an authoritative
    entity citation of this build."""
    project = f"globtest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        real = await _entity(writer, "Real")
        bogus = uuid.uuid4()  # no entity row anywhere
        await _report(writer, "mixed", 5.0, member_ids=[real, bogus])
        await _report(writer, "orphaned", 9.0, member_ids=[bogus])
        await conn.commit()
        await conn.execute(
            builds.update().where(builds.c.id == writer.build_id).values(status="active")
        )
        await conn.commit()

        repo = await BuildScopedRepo.for_active_build(conn, project)
        response = await global_summary(repo, "q", 10)
        _VALIDATOR.validate(response.to_dict())
        assert [r.title for r in response.results] == ["mixed"]  # orphaned dropped whole
        assert [ref.id for ref in response.results[0].source_refs] == [str(real)]
        assert "PARTIAL_RESULTS" in [w.code for w in response.warnings]
    finally:
        await _cleanup(project)
