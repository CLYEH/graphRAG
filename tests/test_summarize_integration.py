"""Why: the summarize step is only correct on the real store — the CHECK
(members citeable), the scope injection (DR-006), and the rerun skip must hold
against live Postgres, through the REAL writer with its per-write building
guard. The partition/parse logic is unit-tested with fakes; here the whole
step runs end-to-end: seeded active graph → Leiden → LLM (deterministic fake)
→ community_reports rows → rerun writes nothing new.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from llama_index.core.llms import LLM
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.resolve import fingerprints
from core.stores.repo import BuildScopedWriter
from core.stores.tables import builds, community_reports, entities, relations
from core.summarize.communities import summarize_build
from tests.conftest import ensure_project

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)


class _FakeLLM:
    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        answer = json.dumps(
            {"title": "Cluster", "summary": "Entities that work together.", "rating": 5}
        )
        return SimpleNamespace(message=SimpleNamespace(content=answer))


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
    await ensure_project(conn, project)
    build_id: uuid.UUID = (
        await conn.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(conn, project, build_id)


async def _entity(writer: BuildScopedWriter, name: str) -> tuple[uuid.UUID, str]:
    key = fingerprints.entity_key("org", name)
    entity_id = uuid.uuid4()
    await writer.insert(
        entities,
        id=entity_id,
        type="org",
        canonical_name=name,
        entity_key=key,
        status="active",
        review_status="unreviewed",
        created_by="rule",
        created_at=NOW,
        updated_at=NOW,
    )
    return entity_id, key


async def _relation(
    writer: BuildScopedWriter, src: tuple[uuid.UUID, str], dst: tuple[uuid.UUID, str]
) -> None:
    await writer.insert(
        relations,
        id=uuid.uuid4(),
        src_entity_id=src[0],
        dst_entity_id=dst[0],
        type="linked",
        relation_signature=fingerprints.relation_signature(src[1], "linked", dst[1]),
        status="active",
        review_status="unreviewed",
        created_by="rule",
        confidence=1.0,
        created_at=NOW,
        updated_at=NOW,
    )


async def _cleanup(project: str) -> None:
    engine = _engine()
    async with engine.connect() as connection:
        await connection.execute(entities.delete().where(entities.c.project == project))
        await connection.execute(
            community_reports.delete().where(community_reports.c.project == project)
        )
        await connection.execute(builds.delete().where(builds.c.project == project))
        await connection.commit()
    await engine.dispose()


async def test_summarize_writes_scoped_citeable_reports_and_reruns_clean(
    conn: AsyncConnection,
) -> None:
    project = f"sumtest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        a = await _entity(writer, "Acme")
        b = await _entity(writer, "BobCo")
        c = await _entity(writer, "CarolInc")
        await _relation(writer, a, b)
        await _relation(writer, b, c)
        await _relation(writer, a, c)
        await _entity(writer, "Loner")  # isolated — a singleton, no report
        await conn.commit()

        report = await summarize_build(writer, cast(LLM, _FakeLLM()))
        await conn.commit()
        assert report.communities == 1 and report.written == 1

        rows = (
            await conn.execute(
                sa.select(community_reports).where(community_reports.c.project == project)
            )
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        # DR-006: the scope was injected by the writer, not passed by the step
        assert row.project == project and row.build_id == writer.build_id
        assert row.level == 0 and row.title == "Cluster"
        # §27.2: the members ARE the citation refs — present and complete
        assert set(row.member_entity_ids) == {a[0], b[0], c[0]}
        assert row.rating == 5.0

        # §5 rerun: the same graph resolves to the same member set → skipped
        rerun = await summarize_build(writer, cast(LLM, _FakeLLM()))
        await conn.commit()
        assert rerun.written == 0
        assert [o.status for o in rerun.outcomes] == ["skipped"]
        total = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(community_reports)
                .where(community_reports.c.project == project)
            )
        ).scalar_one()
        assert total == 1  # nothing duplicated
    finally:
        await _cleanup(project)
