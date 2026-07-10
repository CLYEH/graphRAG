"""Why: DR-006's promise is structural — a caller CANNOT produce an unscoped
read or a cross-build write through this layer, cannot reach the raw
connection it would need to bypass it, and tables whose scoping would be a
lie (cross-build by design, or only transitively scoped) are rejected instead
of faked. These tests pin that structure at the SQL level (via the shared
internal builders — execution needs live Postgres and is covered by the
integration tests through the public surface).
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import repo as repo_module
from core.stores import tables
from core.stores.repo import (
    BUILD_ONLY_SCOPED,
    PROJECT_AND_BUILD_SCOPED,
    BuildNotWritableError,
    BuildScopedRepo,
    BuildScopedWriter,
    NoActiveBuildError,
    NotBuildScopedError,
)

_BUILD = uuid.uuid4()


def _repo() -> BuildScopedRepo:
    # unit tests never execute SQL, so the connection is a placeholder; the
    # internal token is the documented test seam past the factory validation
    # (which needs live Postgres and is integration-tested)
    return BuildScopedRepo(
        cast(AsyncConnection, object()), "p1", _BUILD, _token=repo_module._CONSTRUCTION_TOKEN
    )


def _writer() -> BuildScopedWriter:
    return BuildScopedWriter(
        cast(AsyncConnection, object()), "p1", _BUILD, _token=repo_module._CONSTRUCTION_TOKEN
    )


def test_reads_inject_both_scope_columns() -> None:
    """§27.1: reads automatically filter build_id (and project where the
    table carries it) — the caller cannot forget what it never had to add."""
    for table in PROJECT_AND_BUILD_SCOPED:
        sql = str(_repo()._select(table))
        assert f"{table.name}.build_id = " in sql, table.name
        assert f"{table.name}.project = " in sql, table.name


def test_build_only_tables_inject_build_id() -> None:
    """chunks/relation_evidence carry no project column (§4) — their project
    is derivable through the composite FK parent; the repo scopes what the
    table actually has."""
    for table in BUILD_ONLY_SCOPED:
        sql = str(_repo()._select(table))
        assert f"{table.name}.build_id = " in sql, table.name
        assert "project" not in sql, table.name


@pytest.mark.parametrize(
    "table",
    [tables.builds, tables.review_ledger, tables.entity_mentions, tables.pipeline_runs],
    ids=lambda t: str(t.name),
)
def test_unscopable_tables_are_rejected_loudly(table: sa.Table) -> None:
    """builds is the scope's source of truth; review_ledger is deliberately
    cross-build (DR-003); pipeline_runs has its own §27.7 binding rules;
    entity_mentions are scoped only through their entity. Pretending to scope
    any of these would fake the DR-006 guarantee, so both paths refuse."""
    with pytest.raises(NotBuildScopedError):
        _repo()._select(table)
    with pytest.raises(NotBuildScopedError):
        _repo()._insert_values(table, {"project": "p1"})


def test_writes_inject_the_bound_scope() -> None:
    """§27.1: 寫入一律指定 building 的 build_id — the repo bound to that build
    injects it, so a pipeline writer cannot land rows in another build."""
    insert = _repo()._insert_values(tables.documents, {"source_uri": "s3://d", "content_hash": "c"})
    compiled = insert.compile()
    assert compiled.params["build_id"] == _BUILD
    assert compiled.params["project"] == "p1"


def test_writes_revalidate_the_building_status_inside_the_statement() -> None:
    """§27.1's guarantee must hold PER STATEMENT, not per binding: a build
    that stops being `building` after the writer was bound (activated, failed,
    archived) must not keep absorbing writes. The status recheck is folded
    INTO the insert (INSERT .. SELECT .. WHERE EXISTS) so check and write are
    atomic, and FOR SHARE on the builds row makes in-flight writes and the
    activation UPDATE mutually exclusive — a lock, not a race window (the
    live blocking behavior is integration-tested)."""
    insert = _repo()._insert_values(tables.documents, {"source_uri": "s3://d", "content_hash": "c"})
    # compile with the real dialect the app runs on — the generic compiler
    # renders every with_for_update flavor as FOR UPDATE, hiding the SHARE
    pg_dialect = sa.engine.url.make_url("postgresql+asyncpg://").get_dialect()()
    sql = str(insert.compile(dialect=pg_dialect))
    assert "EXISTS" in sql
    assert "builds.status" in sql
    assert "FOR SHARE" in sql


def test_conflicting_explicit_scope_is_a_loud_bug() -> None:
    """A caller passing a DIFFERENT build_id/project than the binding is a
    cross-build write either way it would be resolved — reject, don't pick."""
    with pytest.raises(ValueError, match="conflict"):
        _repo()._insert_values(tables.documents, {"build_id": uuid.uuid4(), "source_uri": "s"})
    # matching explicit values are redundant but not a bug
    insert = _repo()._insert_values(tables.documents, {"build_id": _BUILD, "source_uri": "s"})
    assert insert.compile().params["build_id"] == _BUILD


def test_consumers_cannot_reach_the_connection_or_mutate_the_scope() -> None:
    """The DR-006 boundary: a consumer holding a repo holds NO raw connection
    (the attribute is name-mangled private and absent from the public
    surface), and the scope has no setters — bypass or drift must be a
    deliberate reach into private state, never a convenience."""
    repo = _repo()
    with pytest.raises(AttributeError):
        _ = repo.conn  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        repo.build_id = uuid.uuid4()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        repo.project = "other"  # type: ignore[misc]
    # __slots__: no __dict__ to smuggle new state through either
    with pytest.raises(AttributeError):
        repo.escape_hatch = object()  # type: ignore[attr-defined]


def test_active_bound_repos_cannot_insert() -> None:
    """§27.1: the active build is an immutable live snapshot — the READ type
    simply has no insert method, so 'this object can write' and 'this scope
    is a verified building build' are the same fact, by type."""
    assert not hasattr(_repo(), "insert")
    assert hasattr(_writer(), "insert")
    # and the writer inherits (never overrides) the read-side factory, whose
    # return type is pinned to the read-only class — so even
    # BuildScopedWriter.for_active_build(...) cannot mint an active-bound writer
    assert "for_active_build" not in vars(BuildScopedWriter)


def test_textual_predicates_are_rejected_and_structural_or_stays_grouped() -> None:
    """SQLAlchemy splices text() into the WHERE conjunction WITHOUT parens, so
    text("1=1 OR ...") flips precedence and reads outside the scope (verified
    by compilation below); structural or_() self-groups. fetch_all therefore
    rejects TextClause — the narrow-only guarantee must hold for EVERY
    accepted input, not just polite ones."""
    # the attack really exists: unparenthesized OR after the ANDed scope
    attacked = _repo()._select(tables.documents).where(sa.text("1=1 OR x"))
    assert " AND 1=1 OR x" in str(attacked.whereclause)
    # structural or_ is parenthesized — cannot escape
    grouped = (
        _repo()
        ._select(tables.documents)
        .where(sa.or_(tables.documents.c.mime == "a", tables.documents.c.mime == "b"))
    )
    assert "AND (documents.mime" in str(grouped.whereclause)


async def test_fetch_all_refuses_raw_sql_predicates() -> None:
    """The guard lives on the public path, before any SQL is built — and it
    covers BOTH raw-SQL vectors: text() and literal_column() compile to the
    byte-identical unparenthesized splice (the latter is a ColumnClause with
    is_literal=True, not a TextClause — a sibling API with the same hole)."""
    attacked = _repo()._select(tables.documents).where(sa.literal_column("1=1 OR x"))
    assert " AND 1=1 OR x" in str(attacked.whereclause)  # the sibling attack is real
    attacks: tuple[sa.ColumnExpressionArgument[bool], ...] = (
        sa.text("1=1 OR true"),
        sa.literal_column("1=1 OR true"),
    )
    for attack in attacks:
        with pytest.raises(TypeError, match="raw-SQL"):
            await _repo().fetch_all(tables.documents, attack)


async def test_fetch_page_refuses_raw_sql_in_both_expression_channels() -> None:
    """WHY (class 9): fetch_page adds a NEW expression channel — ORDER BY —
    and a raw fragment there is the same unparenthesized splice the predicate
    guard exists to close (it cannot widen the WHERE, but it is arbitrary SQL
    text in the statement). Both channels must reject; the guard cannot cover
    only the surface it was first written for."""
    attacks: tuple[sa.ColumnExpressionArgument[bool], ...] = (
        sa.text("1=1 OR true"),
        sa.literal_column("1=1 OR true"),
        # nesting must not hide the raw node from the NEW channel either —
        # the C1b lesson (guards recurse) re-pinned on order_by
        sa.desc(sa.text("1=1 OR true")),
        sa.asc(sa.literal_column("1=1 OR true")),
    )
    for attack in attacks:
        with pytest.raises(TypeError, match="raw-SQL"):
            await _repo().fetch_page(
                tables.documents, attack, order_by=[tables.documents.c.id.desc()], limit=1
            )
        with pytest.raises(TypeError, match="raw-SQL"):
            await _repo().fetch_page(tables.documents, order_by=[attack], limit=1)


def test_fetch_page_builds_scoped_ordered_capped_sql() -> None:
    """The paged read keeps the injected scope AND carries the caller's order
    + cap — structural pin at the SQL level (execution is integration-tested).
    Safe structural expressions (col.desc()/asc()) pass the guard: rejecting
    them would over-block the only legitimate ordering channel (class 9 dual).
    """
    docs = tables.documents
    query = _repo()._select(docs).where(docs.c.id < _BUILD)
    for expression in (docs.c.id.desc(),):
        repo_module._reject_raw_sql(expression)  # accepted, not over-blocked
        query = query.order_by(expression)
    sql = str(query.limit(7).compile(compile_kwargs={"literal_binds": False}))
    assert "documents.build_id = " in sql and "documents.project = " in sql
    assert "ORDER BY documents.id DESC" in sql
    assert "LIMIT" in sql


async def test_fetch_all_refuses_raw_sql_nested_inside_structural_operators() -> None:
    """A top-level TextClause check is not enough: sa.or_(text(...), col==x)
    buries the raw node one level down, but SQLAlchemy still splices it
    verbatim — the ')...--' payload lexically closes the or_ group and the
    tail escapes the ANDed scope (proven by the compiled string below). The
    guard must recurse, so it rejects the raw node wherever it hides."""
    payload = "1=1) OR documents.build_id <> :b --"
    # the buried attack really escapes: the OR lands OUTSIDE the scope's AND
    escaped = (
        _repo()
        ._select(tables.documents)
        .where(sa.or_(sa.text(payload), tables.documents.c.mime == "x"))
    )
    compiled = str(escaped.whereclause)
    assert ") OR documents.build_id <> :b --" in compiled

    nested_attacks: tuple[sa.ColumnExpressionArgument[bool], ...] = (
        sa.or_(sa.text(payload), tables.documents.c.mime == "x"),
        sa.or_(sa.literal_column(payload), tables.documents.c.mime == "x"),
        sa.and_(
            tables.documents.c.mime == "a", sa.or_(sa.text("1=1"), tables.documents.c.mime == "b")
        ),
    )
    for attack in nested_attacks:
        with pytest.raises(TypeError, match="raw-SQL"):
            await _repo().fetch_all(tables.documents, attack)
    # a bare string predicate can't sneak past either — SQLAlchemy 2.x refuses
    # to auto-text() it, so the coercion the guard runs rejects it up front
    with pytest.raises(sa.exc.ArgumentError):
        await _repo().fetch_all(
            tables.documents, cast(sa.ColumnExpressionArgument[bool], "1=1 OR x")
        )


async def test_fetch_all_refuses_custom_operator_predicates() -> None:
    """op()/bool_op() are a THIRD verbatim-splice vector: the operator string
    is emitted raw between the operands and lives on BinaryExpression.operator
    (a custom_op), which text()/literal_column() node checks never see. A
    ')...' payload closes SQLAlchemy's auto-group so the OR escapes the scope
    (proven below); the guard must inspect operators, not just node types."""
    # the escape is real: the injected ')' closes SQLAlchemy's auto-added group
    # right after the left operand, so the trailing OR lands OUTSIDE the scope's
    # AND — `build_id = :b AND (documents.mime ) OR true OR ( :mime_1)`
    escaped = (
        _repo()
        ._select(tables.documents)
        .where(tables.documents.c.mime.op(") OR true OR (")("ignored"))
    )
    compiled = str(escaped.whereclause)
    assert "(documents.mime ) OR true" in compiled  # group closed, OR now top-level

    # every unsafe form (paren-close, keyword payload, bare letters, nested) is
    # rejected — the operator string is what carries the injection
    attacks: tuple[sa.ColumnExpressionArgument[bool], ...] = (
        tables.documents.c.mime.op(") OR true OR (")("ignored"),
        tables.documents.c.mime.bool_op("= 'x' OR true")("ignored"),
        sa.or_(tables.documents.c.mime.op("OP")("y"), tables.documents.c.mime == "z"),
    )
    for attack in attacks:
        with pytest.raises(TypeError, match="raw-SQL"):
            await _repo().fetch_all(tables.documents, attack)


def test_safe_dialect_custom_operators_are_accepted() -> None:
    """The custom_op guard must NOT be a blanket ban: SQLAlchemy renders safe
    PostgreSQL dialect operators (JSONB ->>/@>/?, array &&) as custom_op too,
    and C4+ query adapters need them to filter JSONB/array columns. A symbol-
    only opstring cannot contain a space, keyword, quote or ')', so it cannot
    restructure the boolean expression — only opstrings with characters
    OUTSIDE the PG operator set are the injection vector we reject. Regression
    guard so hardening the boundary never silently breaks real filtering."""
    meta = tables.documents.c.metadata  # a JSONB column on a scoped table
    safe: tuple[sa.ColumnExpressionArgument[bool], ...] = (
        meta["k"].astext == "v",  # ->>
        meta.contains({"a": 1}),  # @>
        meta.has_key("k"),  # ?
    )
    for predicate in safe:
        # does not raise — and stays inside the scope when composed
        repo_module._reject_raw_sql(predicate)
        composed = str(_repo()._select(tables.documents).where(predicate))
        assert "documents.build_id = " in composed


def test_direct_construction_is_fenced_off() -> None:
    """The factories are the only sanctioned bindings — for_active_build
    resolves the scope, for_building_build VALIDATES it (§27.1). A public
    constructor accepting any UUID would reopen the bind-to-anything hole
    the write factory exists to close."""
    with pytest.raises(TypeError, match="for_active_build"):
        BuildScopedRepo(cast(AsyncConnection, object()), "p1", uuid.uuid4())


def test_build_not_writable_error_is_typed() -> None:
    """Pipeline orchestration needs to distinguish 'wrong build' cleanly —
    type plus fields, not string parsing; status None = no such build."""
    build = uuid.uuid4()
    err = BuildNotWritableError("p1", build, "active")
    assert (err.project, err.build_id, err.status) == ("p1", build, "active")
    assert isinstance(err, LookupError)


def test_no_active_build_error_is_typed_and_carries_the_project() -> None:
    """The API layer maps this to the frozen NO_ACTIVE_BUILD code (§15) — it
    needs the type and the project, not a string to parse."""
    err = NoActiveBuildError("p1")
    assert err.project == "p1"
    assert isinstance(err, LookupError)


# --- C4 mutation surface (update/delete/repoint) — SQL shape + guards --------


class _MutConn:
    """Fake conn for the mutation happy/zero-rows paths (no SQL executed)."""

    def __init__(self, rowcount: int, build_status: str | None = "building") -> None:
        self._rowcount = rowcount
        self._status = build_status
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _MutConn:
        self.statements.append(statement)
        return self

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def scalar_one_or_none(self) -> str | None:
        return self._status


def _mut_writer(conn: _MutConn) -> BuildScopedWriter:
    return BuildScopedWriter(
        cast(AsyncConnection, conn), "p1", _BUILD, _token=repo_module._CONSTRUCTION_TOKEN
    )


def test_update_injects_scope_and_building_guard_in_the_statement() -> None:
    """The mutation carries the SAME per-statement invariant as insert: the
    row's full scope AND the building-status EXISTS/FOR SHARE guard live in
    the UPDATE itself — not in a separate check that could race (TOCTOU)."""
    row_id = uuid.uuid4()
    where = _writer()._scoped_where(tables.entities, row_id)
    statement = tables.entities.update().where(*where).values(status="merged")
    pg_dialect = sa.engine.url.make_url("postgresql+asyncpg://").get_dialect()()
    sql = str(statement.compile(dialect=pg_dialect))
    assert "entities.id = " in sql and "entities.build_id = " in sql
    assert "entities.project = " in sql
    assert "EXISTS" in sql and "builds" in sql and "FOR SHARE" in sql


async def test_update_refuses_scope_columns_and_names_the_zero_rows_cause() -> None:
    """Scope columns are unupdatable (a re-scope is a cross-build write in
    disguise); zero rows while the build is still building is the OTHER
    cause — a foreign/unknown row — and gets the typed cross-scope error."""
    with pytest.raises(ValueError, match="scope columns"):
        await _mut_writer(_MutConn(1)).update(tables.entities, uuid.uuid4(), build_id=uuid.uuid4())

    await _mut_writer(_MutConn(1)).update(tables.entities, uuid.uuid4(), status="merged")  # ok

    with pytest.raises(repo_module.RowNotInBuildError):
        await _mut_writer(_MutConn(0, "building")).update(
            tables.entities, uuid.uuid4(), status="merged"
        )
    with pytest.raises(repo_module.BuildNotWritableError):
        await _mut_writer(_MutConn(0, "active")).update(
            tables.entities, uuid.uuid4(), status="merged"
        )


async def test_delete_uses_the_same_guarded_shape() -> None:
    """delete exists only for §27.4 true-duplicate evidence; it must be
    exactly as fenced as update — scoped row + building guard, typed on
    zero rows."""
    await _mut_writer(_MutConn(1)).delete(tables.relation_evidence, uuid.uuid4())  # ok
    with pytest.raises(repo_module.RowNotInBuildError):
        await _mut_writer(_MutConn(0, "building")).delete(tables.relation_evidence, uuid.uuid4())
    with pytest.raises(repo_module.BuildNotWritableError):
        await _mut_writer(_MutConn(0, "failed")).delete(tables.relation_evidence, uuid.uuid4())
