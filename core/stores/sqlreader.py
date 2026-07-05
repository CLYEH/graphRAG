"""Build-scoped executor for GUARDED read-only SQL (DESIGN §21/§27.6, DR-006, C6b).

:class:`~core.stores.repo.BuildScopedRepo` structurally FORBIDS raw SQL — that is
how DR-006 keeps the query layer from forgetting a ``build_id``. The SQL
retrieval mode needs raw SQL by definition, so this is the sanctioned,
fenced alternative: it runs ONLY a statement that already passed
:func:`core.query.sql_guard.validate_sql`, and it never lets that statement
touch a base table.

The reconstruction is the key structural move. The logical structured tables the
whitelist names (``orders`` …) do not physically exist — C2 stored each source
row as JSON in ``documents.raw`` with ``metadata = {table, pk}``. So for the one
table a validated query reads, this executor materialises a build-scoped CTE OF
THE SAME NAME over ``documents`` — ``build_id``/``project``/``table`` injected as
bound parameters — and attaches it to the query. In Postgres a CTE shadows any
base table of that name, so ``SELECT * FROM orders`` resolves to the
build-scoped reconstruction, never a real table: the scope is injected
STRUCTURALLY (every row seen carries the active build_id), exactly the DR-006
guarantee the repo gives its own reads, and even a mis-whitelisted core-table
name (``documents``) would only reconstruct itself, never expose base rows.

Column identifiers are derived from the build's own JSON keys and rendered
through sqlglot (quoted/escaped), so an untrusted key like ``id"); drop`` becomes
a safely quoted identifier, never SQL. This is a READER: it opens no write path,
and in production runs under a dedicated read-only role (that role + its engine
are deferred infra — the executor takes an injected connection, C6a-style).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlglot import exp

from core.query.sql_guard import ValidatedSql
from core.stores.repo import active_build_id

_DIALECT = "postgres"
_SQL_READER_TOKEN = object()

#: The executor's own scope placeholders — a unique prefix so converting them to
#: SQLAlchemy's ``:name`` style (sqlglot renders postgres params as ``%(name)s``)
#: can never touch a user value; the guardrail rejects user placeholders anyway.
_PARAM_RE = re.compile(r"%\((__g_[a-z_]+)\)s")


def _to_named_params(sql: str) -> str:
    """sqlglot renders postgres bind params as ``%(name)s``; SQLAlchemy
    :func:`text` wants ``:name``. Only the executor's own ``__g_``-prefixed
    params exist here (user placeholders are rejected upstream), so this
    conversion is unambiguous."""
    return _PARAM_RE.sub(r":\1", sql)


def _json_text(column: str, key: str) -> exp.Expr:
    """``JSON_EXTRACT_PATH_TEXT(CAST(<column> AS JSON), '<key>')`` — the JSON
    value at ``key`` as text. Built as an AST node (never string-concatenated),
    so an UNTRUSTED build-derived ``key`` is rendered as a safely ESCAPED string
    literal — the function form escapes it (the ``->>`` operator form does not),
    and JSON (not JSONB) is the type that function accepts (``raw`` is text,
    ``metadata`` is jsonb — both cast to json)."""
    return exp.JSONExtractScalar(
        this=exp.cast(exp.column(column), "json"),
        expression=exp.Literal.string(key),
    )


class BuildScopedSqlReader:
    """Executes one guardrail-validated SELECT against a build-scoped
    reconstruction of the logical structured tables. Construct via
    :meth:`for_active_build`; the connection is name-mangled private (reaching it
    is a deliberate bypass, as in :class:`~core.stores.repo.BuildScopedRepo`)."""

    __slots__ = ("__conn", "__project", "__build_id")

    def __init__(
        self,
        conn: AsyncConnection,
        project: str,
        build_id: uuid.UUID,
        *,
        _token: object = None,
    ) -> None:
        if _token is not _SQL_READER_TOKEN:
            raise TypeError(
                "construct via BuildScopedSqlReader.for_active_build — direct "
                "construction would skip the active-build scope binding"
            )
        self.__conn = conn
        self.__project = project
        self.__build_id = build_id

    @property
    def project(self) -> str:
        return self.__project

    @property
    def build_id(self) -> uuid.UUID:
        return self.__build_id

    @classmethod
    async def for_active_build(cls, conn: AsyncConnection, project: str) -> BuildScopedSqlReader:
        """Bind to the project's active build (DR-001), like the read repo."""
        build = await active_build_id(conn, project)
        return BuildScopedSqlReader(conn, project, build, _token=_SQL_READER_TOKEN)

    def _scope_params(self, table: str) -> dict[str, Any]:
        return {
            "__g_project": self.__project,
            "__g_build": str(self.__build_id),
            "__g_table": table,
        }

    async def column_names(self, table: str) -> tuple[str, ...]:
        """The union of JSON keys across this logical table's rows in the active
        build — the columns the reconstruction exposes (build-scoped).

        The ``__`` prefix is RESERVED for the executor's own citation columns
        (``__row_pk``/``__source_uri``): a source row whose JSON carried a key
        literally named ``__row_pk`` would otherwise shadow the real pk and forge
        the emitted citation, so such keys are dropped from the queryable set."""
        result = await self.__conn.execute(
            text(
                "SELECT DISTINCT jsonb_object_keys(raw::jsonb) AS k FROM documents "
                "WHERE project = :__g_project AND build_id = CAST(:__g_build AS uuid) "
                "AND metadata ->> 'table' = :__g_table"
            ).bindparams(**self._scope_params(table))
        )
        return tuple(sorted(row.k for row in result if not row.k.startswith("__")))

    async def run(
        self, validated: ValidatedSql, max_rows: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run the validated SELECT against the build-scoped reconstruction.

        Returns ``(rows, truncated)``: at most ``max_rows`` row dicts (each
        carrying ``__row_pk`` + ``__source_uri`` + the data columns), and whether
        the ``max_rows`` ceiling clipped the result (§22 TRUNCATED). A logical
        table with no rows in this build yields no columns and thus no results —
        nothing to reconstruct — rather than a query against an empty schema.

        Assumes :meth:`apply_timeout` has bound the policy deadline on this
        connection's transaction (the caller applies it once, before schema
        discovery, so every statement in the path is bounded — not just this one).
        """
        columns = await self.column_names(validated.table)
        if not columns:
            return [], False

        ceiling = self._ceiling(validated.statement, max_rows)
        final = validated.statement.limit(ceiling + 1, copy=True).with_(
            validated.table, as_=self._reconstruction(columns), copy=True
        )
        sql = _to_named_params(final.sql(dialect=_DIALECT))
        result = await self.__conn.execute(
            text(sql).bindparams(**self._scope_params(validated.table))
        )
        rows = [dict(row) for row in result.mappings()]
        # TRUNCATED means the POLICY ceiling clipped the set (§22) — not the
        # query's own smaller LIMIT, which is the caller's deliberate choice.
        truncated = len(rows) > ceiling and ceiling == max_rows
        return rows[:ceiling], truncated

    async def apply_timeout(self, timeout_ms: int) -> None:
        """Bind the policy deadline (§21) to this transaction's statements. Apply
        it ONCE, before any read (schema discovery AND the query), so EVERY
        statement in the SQL path is bounded — an expensive predicate
        (``pg_sleep``) or a broad JSON-key scan over a large table is cancelled at
        the deadline rather than holding the connection to the server default.

        ``SET LOCAL`` is transaction-scoped: it auto-resets at commit/rollback so
        it never leaks to a reused connection, and it covers every subsequent
        statement in the same transaction. ``timeout_ms`` is a validated positive
        int (SET takes no bind params, so it is embedded; ``int()`` keeps it
        non-injectable).

        Assumes a non-AUTOCOMMIT connection: under AUTOCOMMIT ``SET LOCAL`` warns
        and no-ops (each statement is its own transaction), silently re-disabling
        this deadline — so the deferred read-only engine (C8) must not enable it.
        """
        await self.__conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))

    async def rollback(self) -> None:
        """Roll back this connection's transaction. A failed statement (a bad
        query or a ``statement_timeout`` cancel) leaves the transaction ABORTED;
        the SQL path calls this before degrading (§22) so the caller can reuse the
        connection (e.g. a hybrid follow-up read) instead of hitting ``current
        transaction is aborted`` — a degradation must not become a failure."""
        await self.__conn.rollback()

    @staticmethod
    def _ceiling(statement: exp.Select, max_rows: int) -> int:
        """The effective row ceiling: the policy ``max_rows`` unless the query's
        own LIMIT asks for fewer (an explicit smaller LIMIT is the caller's
        choice, not a policy truncation)."""
        limit = statement.args.get("limit")
        if limit is not None and isinstance(limit.expression, exp.Literal):
            try:
                return min(int(limit.expression.name), max_rows)
            except ValueError:
                pass
        return max_rows

    @staticmethod
    def _reconstruction(columns: Sequence[str]) -> exp.Expr:
        """A build-scoped SELECT over ``documents`` exposing this logical table's
        pk + source_uri + one column per JSON key, scoped by bound params. Built
        entirely from AST nodes — untrusted JSON-key columns become escaped
        string literals inside :func:`_json_text`, never re-parsed SQL. The
        logical-table filter is a bound param (``:__g_table``); the CTE takes its
        NAME from the query's table in :meth:`run` (``.with_``)."""
        projections = [
            exp.alias_(_json_text("metadata", "pk"), "__row_pk"),
            exp.alias_(exp.column("source_uri"), "__source_uri"),
            *[exp.alias_(_json_text("raw", column), column, quoted=True) for column in columns],
        ]
        where = exp.and_(
            exp.column("project").eq(exp.Placeholder(this="__g_project")),
            exp.column("build_id").eq(exp.cast(exp.Placeholder(this="__g_build"), "uuid")),
            _json_text("metadata", "table").eq(exp.Placeholder(this="__g_table")),
        )
        return exp.select(*projections).from_("documents").where(where)
