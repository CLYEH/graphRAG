"""Why: BuildScopedSqlReader is the fenced raw-SQL seam — the one place a
guardrail-validated SELECT actually reaches Postgres. Its correctness is
structural, not cosmetic: the query must run against a build-scoped CTE
reconstruction (scope injected, never a base table), untrusted JSON-key columns
must be safely quoted, and the max_rows ceiling must clip + flag. These unit
tests pin the composed SQL and the cap logic with a fake connection (the live
path is proven in the integration test); the SQL text is where a scope-bypass or
an injection would show up.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection

from core.query.sql_guard import GuardrailBlocked, validate_sql
from core.stores.sqlreader import _SQL_READER_TOKEN, BuildScopedSqlReader
from core.stores.tables import STRUCTURED_MIME


class _Canceled(Exception):
    """A DB-API ``orig`` carrying an out-of-range SQLSTATE (22023), as the driver
    raises when ``SET LOCAL statement_timeout`` is given a value past its range."""

    sqlstate = "22023"


_PROJECT = "acme"
_BUILD = __import__("uuid").UUID("7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d")
_ALLOWED = ("orders",)
_BLOCKED = ("insert", "update", "delete", "drop", "alter", "truncate")


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)

    def mappings(self) -> list[Any]:
        return self._rows


class _ScalarResult:
    """A scalar-returning result, as the _has_rows EXISTS probe expects."""

    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class _FakeConn:
    """Routes the reader's reads — the jsonb_object_keys column probe (bound
    text() via ``execute``) and the reconstructed query (fully-literal raw SQL via
    ``exec_driver_sql``) — capturing each so tests can inspect the composed SQL. A
    statement marks the connection in-transaction; rollback() ends it, so tests can
    assert the reader's per-phase timed transaction is opened and closed."""

    def __init__(
        self,
        columns: list[str],
        rows: list[dict[str, Any]],
        by_table: dict[str, list[str]] | None = None,
        raise_on_set: bool = False,
    ) -> None:
        self._columns = columns
        self._rows = rows
        self._by_table = by_table or {}
        self._raise_on_set = raise_on_set
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.driver_sql: list[str] = []
        self.rolled_back = False
        self._in_txn = False

    def in_transaction(self) -> bool:
        return self._in_txn

    async def rollback(self) -> None:
        self.rolled_back = True
        self._in_txn = False

    async def execute(self, clause: Any) -> Any:
        self._in_txn = True  # a statement auto-begins the transaction (even a failing one)
        sql = str(clause)
        params = {name: bp.value for name, bp in clause._bindparams.items()}
        self.executed.append((sql, params))
        if "statement_timeout" in sql and self._raise_on_set:
            # an out-of-range value makes SET LOCAL itself fail (SQLSTATE 22023)
            raise OperationalError(sql, params, _Canceled())
        if "AS tname" in sql:  # the batched columns_by_table probe (table, key) pairs
            return _Result(
                [
                    SimpleNamespace(tname=t, k=col)
                    for t, cols in self._by_table.items()
                    for col in cols
                ]
            )
        if "jsonb_object_keys" in sql:  # single-table column_names
            return _Result([SimpleNamespace(k=col) for col in self._columns])
        if "EXISTS" in sql:  # the _has_rows probe — the table has rows iff we seeded any
            return _ScalarResult(bool(self._rows))
        return _Result(self._rows)

    async def exec_driver_sql(self, sql: str) -> _Result:
        self._in_txn = True
        self.driver_sql.append(sql)
        return _Result(self._rows)


def _reader(conn: _FakeConn) -> BuildScopedSqlReader:
    return BuildScopedSqlReader(
        cast(AsyncConnection, conn), _PROJECT, _BUILD, _token=_SQL_READER_TOKEN
    )


def _row(pk: str, **data: str) -> dict[str, Any]:
    return {"__row_pk": pk, "__source_uri": f"s3://{pk}", **data}


async def test_reconstruction_is_build_scoped_and_never_a_base_table() -> None:
    """The executed query wraps the user SELECT in a CTE of the same name over
    documents, filtered to the active (project, build_id, mime, table) — injected
    as escaped LITERALS (the whole query is bind-free, run raw) — so the read is
    structurally confined to the active build's structured rows, and
    `SELECT * FROM orders` can never reach a real base table."""
    conn = _FakeConn(["id", "amount"], [_row("1", id="1", amount="9")])
    validated = validate_sql("SELECT * FROM orders WHERE amount = '9'", _ALLOWED, _BLOCKED)
    await _reader(conn).run(validated, max_rows=10)

    main_sql = conn.driver_sql[-1]  # executed raw (no bind params), not via text()
    assert 'WITH "orders" AS' in main_sql  # reconstructed (quoted CTE), not the base table
    assert "FROM documents" in main_sql
    assert str(_BUILD) in main_sql  # the active build_id, injected as a literal
    assert f"'{_PROJECT}'" in main_sql  # project scope literal
    assert f"'{STRUCTURED_MIME}'" in main_sql  # structured (row) documents only
    assert "'table')" in main_sql and "= 'orders'" in main_sql  # the logical-table filter
    # every JSON-key column is projected as a safely quoted identifier
    assert "'id') AS \"id\"" in main_sql and "'amount') AS \"amount\"" in main_sql


async def test_a_nonstring_pk_is_gated_out_of_the_citation() -> None:
    """metadata.pk is exposed as __row_pk only when it is a JSON string: the
    reconstruction gates it on jsonb_typeof(...) = 'string'. A corrupt row whose pk
    is a number/object then yields NULL and is dropped (PARTIAL_RESULTS), rather than
    cited by a coerced '123' — ->>/JSON_EXTRACT_PATH_TEXT would silently stringify it
    and _to_results' isinstance check can't see through that coercion (§27.2)."""
    conn = _FakeConn(["id"], [_row("1", id="1")])
    validated = validate_sql("SELECT * FROM orders", _ALLOWED, _BLOCKED)
    await _reader(conn).run(validated, max_rows=10)
    main_sql = conn.driver_sql[-1]
    assert "JSONB_TYPEOF" in main_sql and "= 'string'" in main_sql  # pk gated on json string type
    assert "AS __row_pk" in main_sql


async def test_a_table_name_needing_quotes_is_reconstructed_correctly() -> None:
    """allowed_tables is not restricted to bare identifiers, so a whitelisted name
    with a space (`Order Details`) must work: the CTE and the outer reference are
    the SAME quoted identifier, so the CTE shadows the reference (a raw
    `.with_(<str>)` would raise a ParseError → an uncaught 500)."""
    conn = _FakeConn(["id"], [_row("1", id="1")])
    validated = validate_sql('SELECT * FROM "Order Details"', ("Order Details",), _BLOCKED)
    await _reader(conn).run(validated, max_rows=10)
    main_sql = conn.driver_sql[-1]
    assert 'WITH "Order Details" AS' in main_sql  # CTE quoted to match the reference
    assert 'FROM "Order Details"' in main_sql  # the outer reference, same identifier
    import sqlglot

    assert len(sqlglot.parse(main_sql, dialect="postgres")) == 1  # still one statement


async def test_untrusted_column_names_cannot_inject() -> None:
    """Columns are derived from (untrusted) build JSON keys, so a key carrying a
    quote-and-semicolon must not break out into a second statement — it is
    rendered as an escaped literal + quoted identifier. The property that proves
    containment: the whole composed SQL still parses as exactly ONE statement."""
    import sqlglot

    evil = "k' ; drop table documents; --"  # a single-quote break-out attempt
    conn = _FakeConn([evil], [{"__row_pk": "1", evil: "v"}])
    validated = validate_sql("SELECT * FROM orders", _ALLOWED, _BLOCKED)
    await _reader(conn).run(validated, max_rows=10)
    main_sql = conn.driver_sql[-1]
    assert len(sqlglot.parse(main_sql, dialect="postgres")) == 1  # injection contained
    assert "'k'' ; drop table documents; --'" in main_sql  # the key is an escaped literal


async def test_a_hostile_table_name_cannot_inject() -> None:
    """allowed_tables is operator-configured (a weaker threat than LLM output), but
    defense in depth: even a whitelisted name carrying a quote-and-semicolon is
    rendered as ONE escaped quoted identifier (both the CTE and the reference), so
    it can't break out into a second statement — the same containment as columns."""
    import sqlglot

    evil = 'a" ; drop table documents; --'  # a double-quote break-out attempt
    conn = _FakeConn(["id"], [_row("1", id="1")])
    validated = validate_sql('SELECT * FROM "a"" ; drop table documents; --"', (evil,), _BLOCKED)
    await _reader(conn).run(validated, max_rows=10)
    main_sql = conn.driver_sql[-1]
    assert len(sqlglot.parse(main_sql, dialect="postgres")) == 1  # injection contained
    assert '"a"" ; drop table documents; --"' in main_sql  # the name is an escaped identifier


async def test_a_column_name_past_the_identifier_limit_is_refused() -> None:
    """A data column name longer than PostgreSQL's 63-byte identifier limit would be
    truncated (or collide) as a reconstruction alias, so the row would come back
    under a wrong field name. The reader refuses it (GUARDRAIL_BLOCKED upstream)
    rather than silently corrupt the data — before running any reconstruction."""
    conn = _FakeConn(["a" * 64], [{"__row_pk": "1"}])  # a 64-byte JSON key
    validated = validate_sql("SELECT * FROM orders", _ALLOWED, _BLOCKED)
    with pytest.raises(GuardrailBlocked, match="identifier limit"):
        await _reader(conn).run(validated, max_rows=10)
    assert conn.driver_sql == []  # refused before the reconstruction ran


async def test_a_genuinely_empty_table_yields_no_results_without_a_query() -> None:
    """A table with NO rows in this build (no columns AND _has_rows false) has
    nothing to reconstruct — the reader returns empty and never runs a query
    against an empty schema (which would error on the user's WHERE columns)."""
    conn = _FakeConn([], [])  # no columns, and no rows → truly empty
    validated = validate_sql("SELECT * FROM orders WHERE x = '1'", _ALLOWED, _BLOCKED)
    rows, truncated = await _reader(conn).run(validated, max_rows=10)
    assert rows == [] and truncated is False
    assert conn.driver_sql == []  # the reconstructed query never ran


async def test_rows_with_only_reserved_keys_are_still_cited_not_dropped() -> None:
    """A table whose rows carry only reserved (``__``-prefixed) JSON keys has no
    DATA columns, but the rows EXIST and are citable by pk — so the reconstruction
    runs (citation columns only) and returns them, rather than the empty-table
    short-circuit silently dropping every row."""
    conn = _FakeConn([], [{"__row_pk": "1", "__source_uri": "s3://1"}])  # rows, no data cols
    validated = validate_sql("SELECT * FROM orders", _ALLOWED, _BLOCKED)
    rows, truncated = await _reader(conn).run(validated, max_rows=10)
    assert rows == [{"__row_pk": "1", "__source_uri": "s3://1"}]  # cited, not dropped
    assert truncated is False
    assert conn.driver_sql != []  # the reconstruction ran (rows exist)


async def test_ceiling_clips_and_flags_truncation() -> None:
    """More matching rows than max_rows → the extra are dropped and truncated is
    True (§22 TRUNCATED); the reader fetches one past the ceiling to detect it."""
    conn = _FakeConn(["id"], [_row(str(i), id=str(i)) for i in range(5)])
    validated = validate_sql("SELECT * FROM orders", _ALLOWED, _BLOCKED)
    rows, truncated = await _reader(conn).run(validated, max_rows=3)
    assert len(rows) == 3 and truncated is True
    assert "LIMIT 4" in conn.driver_sql[-1]  # one past the policy ceiling, to detect truncation


async def test_within_ceiling_is_not_truncated() -> None:
    conn = _FakeConn(["id"], [_row("1", id="1"), _row("2", id="2")])
    validated = validate_sql("SELECT * FROM orders", _ALLOWED, _BLOCKED)
    rows, truncated = await _reader(conn).run(validated, max_rows=5)
    assert len(rows) == 2 and truncated is False


async def test_a_smaller_user_limit_is_not_a_policy_truncation() -> None:
    """When the query's own LIMIT (below max_rows) is the binding cap, clipping
    to it is the caller's choice — TRUNCATED (a §22 policy-ceiling signal) must
    NOT fire even though more rows matched — AND run() must fetch EXACTLY that
    LIMIT, not one past it: the probe row exists only to detect the policy ceiling,
    so past a user LIMIT it is pure downside (PG would evaluate WHERE/casts on a row
    the query never asked for, which could degrade the whole result)."""
    conn = _FakeConn(["id"], [_row(str(i), id=str(i)) for i in range(5)])
    validated = validate_sql("SELECT * FROM orders LIMIT 2", _ALLOWED, _BLOCKED)
    rows, truncated = await _reader(conn).run(validated, max_rows=10)
    assert len(rows) == 2 and truncated is False
    main_sql = conn.driver_sql[-1]
    assert "LIMIT 2" in main_sql and "LIMIT 3" not in main_sql  # exactly the user LIMIT, no probe


async def test_a_user_limit_equal_to_the_cap_is_treated_as_the_policy_ceiling() -> None:
    """Boundary: when the query's LIMIT equals max_rows, the caller cap and the policy
    cap coincide — run() treats it as the policy ceiling, probing one past and
    reporting TRUNCATED if more matched (the policy would have clipped there anyway)."""
    conn = _FakeConn(["id"], [_row(str(i), id=str(i)) for i in range(5)])
    validated = validate_sql("SELECT * FROM orders LIMIT 3", _ALLOWED, _BLOCKED)
    rows, truncated = await _reader(conn).run(validated, max_rows=3)
    assert len(rows) == 3 and truncated is True
    assert "LIMIT 4" in conn.driver_sql[-1]  # probes at the coinciding ceiling


async def test_limit_zero_reads_no_probe_row() -> None:
    """LIMIT 0 is the caller explicitly asking for NO rows; run() must emit LIMIT 0,
    not a LIMIT 1 probe it would then read and discard."""
    conn = _FakeConn(["id"], [_row("1", id="1")])
    validated = validate_sql("SELECT * FROM orders LIMIT 0", _ALLOWED, _BLOCKED)
    rows, truncated = await _reader(conn).run(validated, max_rows=10)
    assert rows == [] and truncated is False
    assert "LIMIT 0" in conn.driver_sql[-1]  # no probe row fetched


async def test_for_active_build_refuses_a_dirty_connection() -> None:
    """The reader owns its per-phase transactions, so it must be handed a CLEAN
    connection — one already in a transaction (a caller's open unit) is refused
    LOUDLY rather than silently rolled back (Rule 12 / loaned-clean); the check runs
    before the build lookup, so no query is issued."""
    conn = _FakeConn([], [])
    conn._in_txn = True  # a caller's open transaction on the loaned connection
    with pytest.raises(RuntimeError, match="no open transaction"):
        await BuildScopedSqlReader.for_active_build(cast(AsyncConnection, conn), _PROJECT)
    assert conn.executed == []  # refused before the active-build lookup ran


async def test_timed_transaction_bounds_the_phase_and_ends_it() -> None:
    """Each phase runs in its own short timed transaction: the FIRST statement is
    the SET LOCAL binding the policy deadline (§21), and the exit rollback ends the
    transaction (resetting the deadline, clearing any abort) so the connection is
    released — never held across the LLM call between phases (§22)."""
    conn = _FakeConn(["id"], [])
    async with _reader(conn).timed_transaction(250):
        assert conn.executed[0][0] == "SET LOCAL statement_timeout = 250"  # deadline bound first
        assert conn.in_transaction() is True  # the phase holds the transaction while working
    assert conn.rolled_back is True and conn.in_transaction() is False  # released on exit


async def test_timed_transaction_rolls_back_when_the_set_itself_fails() -> None:
    """If the SET LOCAL statement_timeout itself fails (an out-of-range timeout_ms
    that passed the schema's minimum-only check), it has already auto-begun the
    transaction. The finally must still roll back — otherwise the pooled connection
    is handed back in an aborted transaction and its next user hits 'current
    transaction is aborted'. The DBAPIError still propagates (sql_query degrades it)."""
    conn = _FakeConn([], [], raise_on_set=True)
    with pytest.raises(DBAPIError):
        async with _reader(conn).timed_transaction(99_999_999_999):
            pass  # the SET raises on entry — the body never runs
    assert conn.rolled_back is True and conn.in_transaction() is False  # cleaned up despite failure


async def test_columns_by_table_groups_each_tables_keys_in_one_probe() -> None:
    """Schema discovery batches every whitelisted table into ONE statement (so it is
    bounded by a single statement_timeout, not one per table); the result groups the
    JSON keys per table, dropping the reserved __-prefixed ones."""
    conn = _FakeConn([], [], by_table={"orders": ["amount", "pk", "__row_pk"], "cust": ["pk"]})
    cols = await _reader(conn).columns_by_table(["orders", "cust"])
    assert cols == {"orders": ("amount", "pk"), "cust": ("pk",)}  # per-table, __ dropped
    assert sum("AS tname" in sql for sql, _ in conn.executed) == 1  # a single batched statement


async def test_column_names_reserve_the_internal_namespace() -> None:
    """The __-prefix is reserved for the citation columns; a source JSON key
    named __row_pk is dropped from the queryable set so it can't shadow and forge
    the emitted pk."""
    conn = _FakeConn(["__row_pk", "__source_uri", "amount", "id"], [])
    assert await _reader(conn).column_names("orders") == ("amount", "id")


@pytest.mark.parametrize(
    ("sql", "max_rows", "expected"),
    [
        ("SELECT * FROM orders LIMIT 2", 10, 2),  # smaller user LIMIT is honored
        ("SELECT * FROM orders LIMIT 50", 10, 10),  # user LIMIT above the cap is clamped
        ("SELECT * FROM orders", 10, 10),  # no LIMIT → the policy ceiling
    ],
)
def test_effective_ceiling(sql: str, max_rows: int, expected: int) -> None:
    """The row ceiling is min(policy max_rows, the query's own LIMIT) — a smaller
    LIMIT is the caller's choice, not a policy truncation."""
    validated = validate_sql(sql, _ALLOWED, _BLOCKED)
    assert BuildScopedSqlReader._ceiling(validated.statement, max_rows) == expected


def test_direct_construction_is_refused() -> None:
    """Like the read repo, the reader exists only through its scope-binding
    factory — a hand-built one would carry an unvalidated build."""
    with pytest.raises(TypeError, match="for_active_build"):
        BuildScopedSqlReader(cast(AsyncConnection, _FakeConn([], [])), _PROJECT, _BUILD)
