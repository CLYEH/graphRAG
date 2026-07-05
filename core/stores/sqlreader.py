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

The connection is loaned to the reader CLEAN (:meth:`for_active_build` FAILS LOUD
otherwise) and the reader owns its transaction lifecycle: each phase (schema
discovery, then execution) runs in its own short, deadline-bound
:meth:`timed_transaction`, ended on exit. The reader never rolls back a transaction
it did not open, and it deliberately holds NO transaction across the LLM call that
turns the schema into SQL, so no session sits idle-in-transaction while the model
runs — the C8 read pool hands out clean connections and takes them back between
phases.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlglot import exp

from core.query.sql_guard import GuardrailBlocked, ValidatedSql
from core.stores.repo import active_build_id
from core.stores.tables import STRUCTURED_MIME

_DIALECT = "postgres"
_SQL_READER_TOKEN = object()

#: PostgreSQL's identifier byte limit (NAMEDATALEN - 1). A column alias longer than
#: this is truncated by the server, so a data column whose JSON key exceeds it cannot
#: be exposed as a distinct, faithful SQL column.
_MAX_IDENTIFIER_BYTES = 63


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


def _string_typed_pk() -> exp.Expr:
    """``metadata.pk`` as text, but ONLY when it is a JSON *string* — otherwise NULL.

    ``->>`` / ``JSON_EXTRACT_PATH_TEXT`` coerce ANY json scalar (a number ``123``, a
    bool, an object) to text, which would forge a string pk for a corrupt or
    hand-written row; ``_to_results``' ``isinstance(pk, str)`` check runs on the
    already-coerced text and so cannot see through it. Gating on
    ``jsonb_typeof(metadata->'pk') = 'string'`` keeps the citation HONEST: a
    non-string (or absent) pk yields NULL, so the row is dropped and surfaced as
    PARTIAL_RESULTS — §27.2 requires the source row to actually carry a string
    ``(table, pk)``, not one coerced into being. ``metadata`` is jsonb, so the
    jsonb-native functions apply directly (no cast); the key ``'pk'`` is a fixed
    literal, so no escaping concern."""
    pk = exp.func("jsonb_extract_path", exp.column("metadata"), exp.Literal.string("pk"))
    is_string = exp.func("jsonb_typeof", pk).eq(exp.Literal.string("string"))
    pk_text = exp.func("jsonb_extract_path_text", exp.column("metadata"), exp.Literal.string("pk"))
    return exp.case().when(is_string, pk_text).else_(exp.null())


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
        """Bind to the project's active build (DR-001), like the read repo.

        REQUIRES a clean connection (no open transaction). The reader manages its
        own short per-phase transactions (:meth:`timed_transaction`) and must never
        roll back a transaction it did not open, so rather than silently discarding a
        caller's unit — an audit write, other build-scoped repos sharing the
        connection — it FAILS LOUD when handed a dirty one (Rule 12). The active-build
        lookup runs in its OWN committed transaction, so the connection is clean both
        IN and OUT — even a disabled (MODE_SKIPPED) query that returns before any
        phase leaves nothing open — and the reader never issues a rollback that could
        reach caller-owned state."""
        if conn.in_transaction():
            raise RuntimeError(
                "BuildScopedSqlReader.for_active_build requires a connection with no "
                "open transaction — it owns its per-phase transactions and must not "
                "roll back caller-owned state"
            )
        async with conn.begin():  # the lookup's OWN txn, committed on exit → clean out
            build = await active_build_id(conn, project)
        return BuildScopedSqlReader(conn, project, build, _token=_SQL_READER_TOKEN)

    @classmethod
    def bound_to(
        cls, conn: AsyncConnection, project: str, build_id: uuid.UUID
    ) -> BuildScopedSqlReader:
        """Bind to a build the CALLER already resolved via ``active_build_id``
        (§27.1: one lookup per request — see BuildScopedRepo.bound_to). The
        loaned-clean contract still holds: the connection must carry no open
        transaction (the caller's single lookup must be ended before binding).
        """
        if conn.in_transaction():
            raise RuntimeError(
                "BuildScopedSqlReader.bound_to requires a connection with no open "
                "transaction — end the active-build lookup's transaction first"
            )
        return BuildScopedSqlReader(conn, project, build_id, _token=_SQL_READER_TOKEN)

    def _scope_params(self, table: str) -> dict[str, Any]:
        return {
            "__g_project": self.__project,
            "__g_build": str(self.__build_id),
            "__g_table": table,
            "__g_mime": STRUCTURED_MIME,
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
                "AND mime = :__g_mime AND metadata ->> 'table' = :__g_table"
            ).bindparams(**self._scope_params(table))
        )
        return tuple(sorted(row.k for row in result if not row.k.startswith("__")))

    async def columns_by_table(self, tables: Sequence[str]) -> dict[str, tuple[str, ...]]:
        """The queryable columns for EACH given table, discovered in ONE statement.

        Schema discovery must probe every whitelisted table, but a per-table loop
        would run N statements — and ``statement_timeout`` bounds each SEPARATELY,
        so N large tables could run for N × the policy deadline before the LLM call
        without a timeout ever firing. Batching into a single statement makes the
        whole phase bounded by ONE ``statement_timeout``. Reserved (``__``-prefixed)
        keys are dropped, as in :meth:`column_names`; a table with no rows maps to
        an empty tuple."""
        result = await self.__conn.execute(
            text(
                "SELECT DISTINCT metadata ->> 'table' AS tname, jsonb_object_keys(raw::jsonb) AS k "
                "FROM documents WHERE project = :__g_project "
                "AND build_id = CAST(:__g_build AS uuid) AND mime = :__g_mime "
                "AND metadata ->> 'table' IN :__g_tables"
            ).bindparams(
                bindparam("__g_project", self.__project),
                bindparam("__g_build", str(self.__build_id)),
                bindparam("__g_mime", STRUCTURED_MIME),
                bindparam("__g_tables", list(tables), expanding=True),
            )
        )
        by_table: dict[str, set[str]] = {table: set() for table in tables}
        for row in result:
            if not row.k.startswith("__"):
                by_table.setdefault(row.tname, set()).add(row.k)
        return {table: tuple(sorted(by_table[table])) for table in tables}

    async def _has_rows(self, table: str) -> bool:
        """Whether this logical table has ANY row in the active build — distinct
        from whether it has queryable DATA columns. A row whose JSON carries only
        reserved (``__``-prefixed) or empty keys still EXISTS and is citable by its
        metadata pk, so :meth:`column_names` returning empty must not be read as an
        empty table (which would silently drop those citable rows)."""
        result = await self.__conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM documents WHERE project = :__g_project "
                "AND build_id = CAST(:__g_build AS uuid) AND mime = :__g_mime "
                "AND metadata ->> 'table' = :__g_table)"
            ).bindparams(**self._scope_params(table))
        )
        return bool(result.scalar_one())

    async def run(
        self, validated: ValidatedSql, max_rows: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run the validated SELECT against the build-scoped reconstruction.

        Returns ``(rows, truncated)``: at most ``max_rows`` row dicts (each
        carrying ``__row_pk`` + ``__source_uri`` + the data columns), and whether
        the ``max_rows`` ceiling clipped the result (§22 TRUNCATED). A GENUINELY
        empty table (no rows) short-circuits — nothing to reconstruct, and a user
        WHERE on a data column would only error against an empty schema. But rows
        with only reserved/empty JSON keys have no DATA columns yet still exist and
        are citable by pk, so those are reconstructed (citation columns only) and
        returned, not mistaken for an empty table.

        Runs inside :meth:`timed_transaction` (the caller wraps this phase in it),
        so the policy deadline bounds this statement and the transaction is ended
        on exit — the rows are materialised into Python here, before that exit.
        """
        columns = await self.column_names(validated.table)
        # A column name past PostgreSQL's 63-byte identifier limit would be truncated
        # (or collide with another long key sharing its prefix) when the reconstruction
        # aliases `raw->>'k' AS "k"`, so SELECT * would return the row under a
        # truncated/wrong field name. Refuse rather than silently corrupt the data —
        # a typed GUARDRAIL_BLOCKED, not a wrong answer (§27.6 over-block, never under).
        overlong = next(
            (c for c in columns if len(c.encode("utf-8")) > _MAX_IDENTIFIER_BYTES), None
        )
        if overlong is not None:
            raise GuardrailBlocked(
                f"a structured column name exceeds PostgreSQL's {_MAX_IDENTIFIER_BYTES}-byte "
                f"identifier limit and cannot be safely queried: {overlong[:24]}…"
            )
        if not columns and not await self._has_rows(validated.table):
            return [], False  # a genuinely empty table — nothing to reconstruct

        ceiling = self._ceiling(validated.statement, max_rows)
        # Fetch ONE row past the ceiling ONLY when the POLICY max_rows is the binding
        # cap — that extra row is what detects (and reports) TRUNCATED. When the
        # query's OWN smaller LIMIT is the cap, clipping to it is the caller's choice
        # (never TRUNCATED), so the extra row is pure downside: it makes PostgreSQL
        # evaluate WHERE/casts on a row the query did not ask for — a later
        # `amount::numeric` on a nonnumeric value could degrade the whole result to
        # GUARDRAIL_BLOCKED — and for `LIMIT 0` it would read a row the caller
        # explicitly excluded. So probe only at the policy ceiling.
        probe = ceiling == max_rows
        fetch = ceiling + 1 if probe else ceiling
        # Name the CTE and the outer table reference with the SAME quoted
        # identifier, so the CTE shadows the reference for ANY whitelisted name —
        # a bare `orders` (folds to `"orders"`) or a `"Order Details"` that
        # `.with_(<str>)` can't even parse — and the identifier escaping contains
        # an injection in the name (allowed_tables is not restricted to bare idents).
        alias = exp.to_identifier(validated.table, quoted=True)
        statement = validated.statement.copy()
        table_ref = statement.find(exp.Table)
        assert table_ref is not None  # the guardrail guarantees exactly one table
        table_ref.set("this", alias.copy())
        final = statement.limit(fetch, copy=True).with_(
            alias.copy(), as_=self._reconstruction(validated.table, columns), copy=True
        )
        # The rendered SQL is fully self-contained — scope values and untrusted
        # JSON keys alike are sqlglot-ESCAPED LITERALS (never string-concatenated),
        # so it carries no bind params. Run it RAW via exec_driver_sql: text()
        # would re-scan the whole string for `:name`/`%(x)s` binds and mis-read a
        # DATA colon (`= ':new'`) or a `%(__g_x)s`-shaped literal as an (unbound)
        # parameter; exec_driver_sql passes it to the driver verbatim (asyncpg
        # binds by $N, so `:` and `%` in data are inert).
        result = await self.__conn.exec_driver_sql(final.sql(dialect=_DIALECT))
        rows = [dict(row) for row in result.mappings()]
        # TRUNCATED means the POLICY ceiling clipped the set (§22) — detectable only
        # at the probe (the query's own smaller LIMIT is the caller's deliberate
        # choice, never TRUNCATED).
        truncated = probe and len(rows) > ceiling
        return rows[:ceiling], truncated

    @asynccontextmanager
    async def timed_transaction(self, timeout_ms: int) -> AsyncIterator[None]:
        """Own ONE short, deadline-bound transaction for a single phase of the SQL
        path — schema discovery, then (separately) execution — and END it on exit.

        The connection is loaned to the reader CLEAN (no caller transaction; see
        :meth:`for_active_build`), and the reader manages its own transactions so it
        never holds one across non-DB work: the multi-second LLM call sits BETWEEN
        two of these, with no session left idle-in-transaction (which a concurrent
        C8 read pool — or an ``idle_in_transaction_session_timeout`` — would punish).

        ``SET LOCAL statement_timeout`` binds the policy deadline (§21) to this
        phase's statements; it auto-begins the transaction and covers every
        statement until the exit ``rollback``, which ends the transaction (a read
        persists nothing), resetting the deadline and clearing an aborted statement
        (a timeout cancel / bad query) so the connection is immediately reusable
        (§22). ``timeout_ms`` is a validated positive int (SET takes no bind params,
        so it is embedded; ``int()`` keeps it non-injectable). Assumes a
        non-AUTOCOMMIT connection — under AUTOCOMMIT ``SET LOCAL`` no-ops, silently
        disabling the deadline, so the deferred read-only engine (C8) must not
        enable it."""
        try:
            # The SET is INSIDE the try: it auto-begins the transaction, so if it
            # itself fails (e.g. a timeout_ms past PostgreSQL's accepted range — the
            # policy schema bounds only a minimum), the finally still rolls back the
            # now-aborted transaction, leaving the pooled connection clean for its
            # next user rather than poisoned with "current transaction is aborted".
            await self.__conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            yield
        finally:
            await self.__conn.rollback()  # end the phase's txn — reset deadline, clear aborts

    @staticmethod
    def _ceiling(statement: exp.Select, max_rows: int) -> int:
        """The effective row ceiling: the policy ``max_rows`` unless the query's own
        LIMIT asks for fewer (an explicit smaller LIMIT is the caller's choice, not a
        policy truncation). The guardrail guarantees any LIMIT is a plain integer
        literal (:func:`~core.query.sql_guard._reject_nonliteral_limit`) — a casted
        or computed LIMIT is rejected upstream — so it parses cleanly here."""
        limit = statement.args.get("limit")
        if limit is None:
            return max_rows
        return min(int(limit.expression.name), max_rows)

    def _reconstruction(self, table: str, columns: Sequence[str]) -> exp.Expr:
        """A build-scoped SELECT over ``documents`` exposing this logical table's
        pk + source_uri + one column per JSON key. Built entirely from AST nodes,
        so EVERY value — the scope (project, build_id, table, mime) and the
        untrusted JSON-key columns alike — is a sqlglot-ESCAPED literal, never
        string-concatenated SQL; the whole reconstruction is bind-free, which is
        what lets :meth:`run` execute it raw (see there). The scope is injected
        STRUCTURALLY (every row seen carries the active build_id, DR-006); the CTE
        takes its NAME from ``table`` in :meth:`run` (``.with_``)."""
        projections = [
            exp.alias_(_string_typed_pk(), "__row_pk"),
            exp.alias_(exp.column("source_uri"), "__source_uri"),
            *[exp.alias_(_json_text("raw", column), column, quoted=True) for column in columns],
        ]
        where = exp.and_(
            exp.column("project").eq(exp.Literal.string(self.__project)),
            exp.column("build_id").eq(exp.cast(exp.Literal.string(str(self.__build_id)), "uuid")),
            # only structured (row) documents — a free-text doc that happens to
            # carry {"table": ...} metadata must not leak in (its prose `raw`
            # would break `raw::json`, degrading the whole table's SQL).
            exp.column("mime").eq(exp.Literal.string(STRUCTURED_MIME)),
            _json_text("metadata", "table").eq(exp.Literal.string(table)),
        )
        return exp.select(*projections).from_("documents").where(where)
