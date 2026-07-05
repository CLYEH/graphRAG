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
import sqlglot
from alembic import command
from alembic.config import Config
from llama_index.core.llms import LLM
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlglot import exp

from core.config import get_settings
from core.query.policy import SQL_BLOCKED_KEYWORDS_MIN, TextToSql
from core.query.sql import sql_query
from core.query.sql_guard import ValidatedSql
from core.stores.repo import BuildScopedWriter
from core.stores.sqlreader import BuildScopedSqlReader
from core.stores.tables import STRUCTURED_MIME, builds, documents

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
        mime=STRUCTURED_MIME,
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


async def test_statement_timeout_cancels_but_the_savepoint_spares_the_caller(
    conn: AsyncConnection,
) -> None:
    """The reader enforces the policy deadline as a real Postgres statement_timeout;
    when the cancel aborts the statement, the reader's SAVEPOINT clears it and undoes
    the SET LOCAL — WITHOUT rolling back the caller's surrounding transaction, so the
    caller's uncommitted prior work survives and the connection stays reusable.
    Tested at the reader seam because the guardrail (correctly) rejects pg_sleep —
    the only deterministic way to make a query slow — so we hand the reader a
    ValidatedSql directly to prove SET LOCAL + savepoint recovery on live PG."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        await _row(writer, "orders", "1", customer="acme", amount="9")
        await conn.commit()
        await _activate(conn, writer.build_id)
        await conn.commit()

        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        # the caller has UNCOMMITTED work in its transaction, before SQL retrieval
        await conn.execute(text("CREATE TEMP TABLE caller_marker (x int) ON COMMIT DROP"))
        await conn.execute(text("INSERT INTO caller_marker VALUES (42)"))

        slow = ValidatedSql(
            statement=cast(
                exp.Select,
                sqlglot.parse_one(
                    "SELECT * FROM orders WHERE pg_sleep(5) IS NOT NULL", dialect="postgres"
                ),
            ),
            table="orders",
        )
        async with reader.transaction():
            await reader.apply_timeout(200)  # 200ms deadline vs a 5s sleep, inside the savepoint
            with pytest.raises(DBAPIError) as excinfo:
                await reader.run(slow, max_rows=100)
            assert getattr(excinfo.value.orig, "sqlstate", None) == "57014"  # query_canceled

        # exiting the context rolled back TO the savepoint: the abort is cleared and
        # the SET LOCAL is undone, but the caller's uncommitted row is untouched.
        marker = (await conn.execute(text("SELECT x FROM caller_marker"))).scalar_one()
        assert marker == 42  # the caller's prior work survived the reader's cleanup
        timeout = (
            await conn.exec_driver_sql("SELECT current_setting('statement_timeout')")
        ).scalar_one()
        assert timeout == "0"  # the deadline did not leak past the savepoint
        reusable = (await conn.execute(text("SELECT 1"))).scalar_one()
        assert reusable == 1  # aborted transaction cleared → connection reusable
    finally:
        await _cleanup(project)


async def test_reconstruction_reads_only_structured_documents(conn: AsyncConnection) -> None:
    """A free-text document that happens to carry {"table": "orders"} metadata must
    NOT leak into the logical orders table: its prose `raw` would break `raw::json`
    and degrade the whole table's SQL, or be returned as a fake cited row. The mime
    filter (structured rows only) excludes it, so the query stays clean."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        await _row(writer, "orders", "1", customer="acme", amount="50")
        # a PROSE document masquerading with the same table metadata (mime differs)
        await writer.insert(
            documents,
            id=uuid.uuid4(),
            source_uri="file:///notes.txt",
            content_hash=f"prose:{writer.build_id}",
            mime="text/plain",
            metadata={"table": "orders", "pk": "x"},
            raw="this is prose, not json",
            ingested_at=NOW,
        )
        await conn.commit()
        await _activate(conn, writer.build_id)
        await conn.commit()

        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        response = await sql_query(
            reader, cast(LLM, _FakeLLM("SELECT * FROM orders")), _POLICY, "all orders", 100
        )
        _VALIDATOR.validate(response.to_dict())
        assert response.warnings == ()  # the prose `raw` never reached raw::json
        pks = [r.source_refs[0].metadata["pk"] for r in response.results]
        assert pks == ["1"]  # only the structured row, not the prose doc
    finally:
        await _cleanup(project)


async def test_a_colon_in_a_key_and_a_predicate_literal_execute(conn: AsyncConnection) -> None:
    """A JSON-key column with a colon (`http:status`) and an LLM predicate with a
    colon literal (`= ':special'`) both execute and return the row — the colon
    escaping stops text() mis-reading them as unbound binds (a 500 without it)."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        await writer.insert(
            documents,
            id=uuid.uuid4(),
            source_uri="file:///orders.csv#pk=1",
            content_hash=f"orders:1:{writer.build_id}",
            mime=STRUCTURED_MIME,
            metadata={"table": "orders", "pk": "1"},
            raw=json.dumps(
                {"pk": "1", "customer": ":special", "http:status": "ok"}, sort_keys=True
            ),
            ingested_at=NOW,
        )
        await conn.commit()
        await _activate(conn, writer.build_id)
        await conn.commit()

        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        response = await sql_query(
            reader,
            cast(LLM, _FakeLLM("SELECT * FROM orders WHERE customer = ':special'")),
            _POLICY,
            "the special one",
            100,
        )
        _VALIDATOR.validate(response.to_dict())
        assert response.warnings == ()  # no unbound-parameter 500
        (result,) = response.results
        assert json.loads(result.text or "{}") == {
            "pk": "1",
            "customer": ":special",
            "http:status": "ok",  # the colon key round-trips as a real column
        }
    finally:
        await _cleanup(project)


async def test_a_whitelisted_table_name_needing_quotes_works(conn: AsyncConnection) -> None:
    """allowed_tables is not restricted to bare identifiers, so a real query over a
    whitelisted `Order Details` (space → must be quoted) succeeds: the CTE and the
    outer reference are the same quoted identifier. A raw `.with_(<str>)` would
    raise a ParseError → an uncaught 500."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        await _row(writer, "Order Details", "1", amount="9")
        await conn.commit()
        await _activate(conn, writer.build_id)
        await conn.commit()

        policy = TextToSql(
            enabled=True,
            allowed_tables=("Order Details",),
            blocked_keywords=SQL_BLOCKED_KEYWORDS_MIN,
            max_rows=100,
            timeout_ms=5000,
        )
        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        response = await sql_query(
            reader, cast(LLM, _FakeLLM('SELECT * FROM "Order Details"')), policy, "details", 100
        )
        _VALIDATOR.validate(response.to_dict())
        assert response.warnings == ()
        (result,) = response.results
        assert result.source_refs[0].metadata == {"table": "Order Details", "pk": "1"}
    finally:
        await _cleanup(project)


async def test_a_successful_query_does_not_leak_the_statement_timeout(
    conn: AsyncConnection,
) -> None:
    """sql_query ends its transaction on the success path too, so the SET LOCAL
    statement_timeout (5000ms here) does NOT linger on the connection — a fresh
    statement (a hybrid follow-up read) sees the default, not the SQL deadline."""
    project = f"sqltest-{uuid.uuid4().hex[:10]}"
    try:
        writer = await _new_build(conn, project)
        await _row(writer, "orders", "1", amount="9")
        await conn.commit()
        await _activate(conn, writer.build_id)
        await conn.commit()

        reader = await BuildScopedSqlReader.for_active_build(conn, project)
        response = await sql_query(
            reader, cast(LLM, _FakeLLM("SELECT * FROM orders")), _POLICY, "all", 100
        )
        assert response.warnings == ()
        leftover = (
            await conn.exec_driver_sql("SELECT current_setting('statement_timeout')")
        ).scalar_one()
        assert leftover == "0"  # reset by the transaction end, not the policy's 5000ms
    finally:
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
