"""Why: sql_query is only correct if a real NL→SQL query, run against a real
build's structured rows, comes back as §16 `row` results cited by (table, pk) —
and NEVER escapes the active build or the read-only shape. The guardrail + the
mapping are unit-tested with fakes; here the whole path runs against live
Postgres with a deterministic fake LLM (no OpenAI key): rows C2-shaped as JSON in
`documents` are reconstructed into a build-scoped CTE, queried, and cited. Build
isolation and end-to-end guardrail rejection are proven where they matter — on
the database.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from llama_index.core.llms import LLM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.query.policy import SQL_BLOCKED_KEYWORDS_MIN, TextToSql
from core.query.sql import sql_query
from core.stores.repo import BuildScopedWriter
from core.stores.sqlreader import BuildScopedSqlReader
from core.stores.tables import builds, documents

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)

_SCHEMA = json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)

_POLICY = TextToSql(
    enabled=True,
    allowed_tables=("orders",),
    blocked_keywords=SQL_BLOCKED_KEYWORDS_MIN,
    max_rows=100,
    timeout_ms=5000,
)


class _FakeLLM:
    """Returns a fixed SQL string — the NL→SQL step is exercised, deterministically
    and without a key; the guardrail + executor are the real path under test."""

    def __init__(self, sql: str) -> None:
        self._sql = sql

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(message=SimpleNamespace(content=self._sql))


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


async def _new_build(connection: AsyncConnection, project: str) -> BuildScopedWriter:
    build_id: uuid.UUID = (
        await connection.execute(
            builds.insert().values(project=project, status="building").returning(builds.c.id)
        )
    ).scalar_one()
    return await BuildScopedWriter.for_building_build(connection, project, build_id)


async def _row(writer: BuildScopedWriter, table: str, pk: str, **data: str) -> None:
    await writer.insert(
        documents,
        id=uuid.uuid4(),
        source_uri=f"file:///{table}.csv#pk={pk}",
        content_hash=f"{table}:{pk}:{writer.build_id}",
        mime="application/json",
        metadata={"table": table, "pk": pk},
        raw=json.dumps({"pk": pk, **data}, sort_keys=True),
        ingested_at=NOW,
    )


async def _activate(connection: AsyncConnection, build_id: uuid.UUID) -> None:
    await connection.execute(builds.update().where(builds.c.id == build_id).values(status="active"))


async def _cleanup(project: str) -> None:
    engine = _engine()
    async with engine.connect() as connection:
        await connection.execute(documents.delete().where(documents.c.project == project))
        await connection.execute(builds.delete().where(builds.c.project == project))
        await connection.commit()
    await engine.dispose()


async def test_nl_sql_returns_cited_rows_end_to_end(conn: AsyncConnection) -> None:
    """A real query over the reconstructed `orders` table returns the matching
    source rows, each cited by (table, pk) with the document's source_uri, and
    the payload validates against the frozen §16 schema."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        await _row(writer, "orders", "1", customer="acme", amount="50")
        await _row(writer, "orders", "2", customer="acme", amount="150")
        await _row(writer, "orders", "3", customer="globex", amount="200")
        await conn.commit()
        await _activate(conn, writer.build_id)
        await conn.commit()

        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        llm = _FakeLLM("SELECT * FROM orders WHERE amount::numeric >= 150 ORDER BY amount::numeric")
        response = await sql_query(reader, cast(LLM, llm), _POLICY, "big orders", 100)

        payload = response.to_dict()
        _VALIDATOR.validate(payload)
        assert response.warnings == ()
        pks = [r["source_refs"][0]["metadata"]["pk"] for r in payload["results"]]
        assert pks == ["2", "3"]  # amount >= 150, in ORDER BY amount (150 then 200)
        first = payload["results"][0]
        assert first["result_type"] == "row"
        assert first["source_refs"][0]["source_uri"] == "file:///orders.csv#pk=2"
        assert json.loads(first["text"]) == {"pk": "2", "customer": "acme", "amount": "150"}
    finally:
        await _cleanup(project)


async def test_query_reads_only_the_active_build(conn: AsyncConnection) -> None:
    """The reconstruction is build-scoped: an archived build's row with the same
    pk coexists in `documents`, but a query bound to the active build returns
    only the active build's row (DR-006)."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        old = await _new_build(conn, project)
        await _row(old, "orders", "1", customer="stale", amount="1")
        await conn.commit()
        new = await _new_build(conn, project)
        await _row(new, "orders", "1", customer="fresh", amount="1")
        await conn.commit()
        await _activate(conn, new.build_id)
        await conn.execute(
            builds.update().where(builds.c.id == old.build_id).values(status="archived")
        )
        await conn.commit()

        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        response = await sql_query(
            reader, cast(LLM, _FakeLLM("SELECT * FROM orders")), _POLICY, "all orders", 100
        )
        _VALIDATOR.validate(response.to_dict())
        customers = [json.loads(r.text or "{}")["customer"] for r in response.results]
        assert customers == ["fresh"]  # never the archived build's row
    finally:
        await _cleanup(project)


async def test_an_expensive_query_is_cancelled_at_the_deadline(conn: AsyncConnection) -> None:
    """A guardrail-valid but expensive query (pg_sleep in the predicate) is
    cancelled by the policy statement_timeout instead of holding the connection —
    it degrades to PARTIAL_RESULTS (§21/§22), proving the deadline is enforced on
    the database, not just carried in the policy."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        await _row(writer, "orders", "1", customer="acme", amount="9")
        await conn.commit()
        await _activate(conn, writer.build_id)
        await conn.commit()

        fast_deadline = TextToSql(
            enabled=True,
            allowed_tables=("orders",),
            blocked_keywords=SQL_BLOCKED_KEYWORDS_MIN,
            max_rows=100,
            timeout_ms=200,  # 200ms deadline vs a 5s sleep → cancelled
        )
        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        response = await sql_query(
            reader,
            cast(LLM, _FakeLLM("SELECT * FROM orders WHERE pg_sleep(5) IS NOT NULL")),
            fast_deadline,
            "slow scan",
            100,
        )
        _VALIDATOR.validate(response.to_dict())
        assert response.results == () and response.warnings[0].code == "PARTIAL_RESULTS"
    finally:
        await conn.rollback()  # clear the cancelled transaction before cleanup
        await _cleanup(project)


async def test_a_write_attempt_is_blocked_end_to_end(conn: AsyncConnection) -> None:
    """If the LLM emits a write, the guardrail rejects it before execution: the
    response is GUARDRAIL_BLOCKED and the documents table is untouched."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        await _row(writer, "orders", "1", customer="acme", amount="9")
        await conn.commit()
        await _activate(conn, writer.build_id)
        await conn.commit()

        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        response = await sql_query(
            reader, cast(LLM, _FakeLLM("DROP TABLE documents")), _POLICY, "drop it", 100
        )
        _VALIDATOR.validate(response.to_dict())
        assert response.results == () and response.warnings[0].code == "GUARDRAIL_BLOCKED"
        # documents is intact — the write never reached the database
        survived = (
            await conn.execute(
                text("SELECT count(*) FROM documents WHERE project = :p"), {"p": project}
            )
        ).scalar_one()
        assert survived == 1
    finally:
        await _cleanup(project)
