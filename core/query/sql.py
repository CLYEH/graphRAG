"""SQL retrieval: NL→SQL over the structured rows → §16 response (§8/§21/§27.6, C6b).

The §8 ``sql`` modality. An LLM turns the question into SQL, the
:mod:`~core.query.sql_guard` guardrail validates it (read-only, single flat
whitelisted-table SELECT), and :class:`~core.stores.sqlreader.BuildScopedSqlReader`
runs it against a build-scoped reconstruction of the logical structured tables
(the rows C2 stored as JSON in ``documents``). Each result row is one source row,
cited by ``(table, pk)`` — the §27.2 ``row`` minimum — which is exactly why the
guardrail restricts the shape to flat ``SELECT *``: an aggregate or a join folds
many source rows into one, and that fold cannot carry a single ``(table, pk)``.

Failure is degradation, never a 500 (§22): the mode disabled yields ``MODE_SKIPPED``,
a guardrail rejection (the classic NL→SQL risk) yields ``GUARDRAIL_BLOCKED`` with
the reason, the ``max_rows`` ceiling yields ``TRUNCATED`` — each a typed warning
over an empty-or-partial result set. Values read back are Postgres data (the SoR,
build-scoped), but the pk each citation rests on is still checked: a row with no
usable pk is dropped rather than emitted uncited (the read/emit discipline C6a
established).

The envelope, ordering, and require_sources invariant come from
:mod:`core.query.results`; the score is positional (a SQL row has no relevance
score), assigned so the query's own ORDER BY survives ``ordered_results``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from llama_index.core.llms import LLM, ChatMessage, MessageRole
from sqlalchemy.exc import DBAPIError

from core.graph.structured import row_source_ref
from core.query.policy import GUARDRAIL_WARNING_CODE, TextToSql
from core.query.results import (
    McpResponse,
    QueryWarning,
    RetrievalResult,
    SourceRef,
    ordered_results,
)
from core.query.sql_guard import GuardrailBlocked, validate_sql
from core.stores.sqlreader import BuildScopedSqlReader

_TOOL = "sql_query"

_SYSTEM = (
    "You translate a question into ONE read-only PostgreSQL query over the given "
    "tables. Reply with only the SQL, no prose, no code fence."
)

_RULES = (
    "Rules:\n"
    "- SELECT * FROM exactly one of the tables above (no column list).\n"
    "- Narrow with WHERE; you may ORDER BY and LIMIT.\n"
    "- No JOIN, subquery, GROUP BY, aggregate, DISTINCT, CTE, or UNION.\n"
    "- Every column is text; cast when comparing (amount::numeric, ts::timestamptz).\n"
)


async def sql_query(
    reader: BuildScopedSqlReader,
    llm: LLM,
    policy: TextToSql,
    query: str,
    max_rows: int,
    *,
    expose_debug: bool = False,
) -> McpResponse:
    """§8 sql retrieval over the active build, as a §16 response.

    ``reader`` is bound to the active build (DR-001); ``policy`` is the resolved
    ``text_to_sql`` block; ``max_rows`` is the caller-reconciled row ceiling
    (``min`` of the top-level ``max_sql_rows`` and ``text_to_sql.max_rows``).

    ``expose_debug`` carries §16's ``debug`` gate down from the caller (which
    combines ``query_policy.expose_debug`` with the caller's role, §21). It
    surfaces the SQL this mode GENERATED from the question — the one fact that
    makes a result of this shape interpretable, because the caller wrote a
    QUESTION and the rows came from a machine translation of it they never
    saw. Without it an empty answer is unreadable: "no matching data" and "the
    model understood you differently" look identical.

    This is PROVENANCE, not an answerability judgement — §19/MCP4 measured the
    latter and refused it (an out-of-domain question outscored in-domain ones,
    so no threshold separates them, and a wrong low-confidence flag is worse
    than none). Saying WHAT WAS RUN needs no such judgement.
    """
    started = time.monotonic()
    if not policy.enabled:
        # no SQL was generated, so there is nothing to show but the decision
        return _response(
            reader,
            query,
            (),
            (_warn("MODE_SKIPPED", "sql mode is disabled"),),
            _debug(
                expose_debug, started, sql=None, note="sql mode is disabled", reached_store=False
            ),
        )

    # Phase 1 — schema discovery in its OWN short timed transaction, ended before
    # the LLM. The deadline bounds the JSON-key scan (a discovery timeout degrades,
    # not 500s), and the connection is released so nothing sits idle-in-transaction
    # across the model latency that follows.
    try:
        async with reader.timed_transaction(policy.timeout_ms):
            schema = await _schema_prompt(reader, policy.allowed_tables)
    except DBAPIError as exc:
        # discovery failed, so no SQL was ever generated
        return _degrade(reader, query, exc, policy.timeout_ms, expose_debug, started, None)

    # The LLM call — NO transaction is held here.
    candidate = _extract_sql(await _ask_llm(llm, schema, query))
    try:
        validated = validate_sql(candidate, policy.allowed_tables, policy.blocked_keywords)
    except GuardrailBlocked as blocked:
        # the REFUSED candidate is the most useful thing to show: the caller
        # can see what their question was translated into and why it was denied
        return _response(
            reader,
            query,
            (),
            (_warn(GUARDRAIL_WARNING_CODE, blocked.reason),),
            _debug(expose_debug, started, sql=candidate, note="refused by the guardrail"),
        )

    # Phase 2 — execution in a FRESH short timed transaction, ended on exit (which
    # resets the deadline and clears any aborted statement).
    try:
        async with reader.timed_transaction(policy.timeout_ms):
            rows, truncated = await reader.run(validated, max_rows)
    except GuardrailBlocked as blocked:
        # run() refuses a query it can't execute FAITHFULLY (e.g. a data column name
        # past the identifier limit) — a typed rejection, not a wrong answer.
        return _response(
            reader,
            query,
            (),
            (_warn(GUARDRAIL_WARNING_CODE, blocked.reason),),
            _debug(
                expose_debug,
                started,
                sql=validated.statement.sql(dialect="postgres"),
                note="refused at execution",
            ),
        )
    except DBAPIError as exc:
        return _degrade(
            reader,
            query,
            exc,
            policy.timeout_ms,
            expose_debug,
            started,
            validated.statement.sql(dialect="postgres"),
        )

    results, dropped = _to_results(rows, validated.table)
    warnings: list[QueryWarning] = []
    if truncated:
        warnings.append(_warn("TRUNCATED", f"result truncated to the {max_rows}-row ceiling (§21)"))
    if dropped:
        # rows the DB returned but that carry no citable (table, pk) — §27.2 forbids
        # emitting them uncited, and §22 forbids hiding the shortfall as a complete
        # answer, so the caller learns the result is partial.
        warnings.append(
            _warn("PARTIAL_RESULTS", f"{dropped} row(s) omitted — no citable (table, pk) (§27.2)")
        )
    return _response(
        reader,
        query,
        results,
        tuple(warnings),
        _debug(
            expose_debug,
            started,
            sql=validated.statement.sql(dialect="postgres"),
            note=f"{len(results)} row(s)",
        ),
    )


def _degrade(
    reader: BuildScopedSqlReader,
    query: str,
    exc: DBAPIError,
    timeout_ms: int,
    expose_debug: bool,
    started: float,
    sql: str | None,
) -> McpResponse:
    """Map a DB failure to a typed degradation (§22), never a 500 — the phase's
    timed_transaction rolls the (possibly aborted) statement back on the way out. A
    bug in our own SQL composition is NOT a DBAPIError, so it never reaches here —
    it propagates and fails loud (Rule 12)."""
    if _is_timeout(exc):
        # the policy deadline cancelled it — the answer is incomplete (§22
        # "逾時：回部分結果 + warning"), distinct from an invalid query.
        return _response(
            reader,
            query,
            (),
            (_warn("PARTIAL_RESULTS", f"query exceeded the {timeout_ms}ms deadline (§21)"),),
            _debug(expose_debug, started, sql=sql, note="deadline exceeded"),
        )
    # otherwise an LLM-hallucinated column / bad cast — the query is unusable.
    return _response(
        reader,
        query,
        (),
        (_warn(GUARDRAIL_WARNING_CODE, f"the query could not be executed ({type(exc).__name__})"),),
        _debug(expose_debug, started, sql=sql, note="not executable"),
    )


async def _ask_llm(llm: LLM, schema: str, query: str) -> str:
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=_SYSTEM),
        ChatMessage(role=MessageRole.USER, content=f"{schema}\n{_RULES}\nQuestion: {query}"),
    ]
    response = await llm.achat(messages)
    return response.message.content or ""


async def _schema_prompt(reader: BuildScopedSqlReader, allowed_tables: tuple[str, ...]) -> str:
    """List each whitelisted table with the columns it actually has in this build,
    so the LLM writes SQL against real column names. Discovered in ONE batched
    statement (not one per table) so the whole phase is bounded by a single
    statement_timeout — N large tables can't run for N × the policy deadline."""
    columns_by_table = await reader.columns_by_table(allowed_tables)
    lines = ["Tables (all columns are text):"]
    for table in allowed_tables:
        columns = columns_by_table[table]
        lines.append(f"- {table}({', '.join(columns)})" if columns else f"- {table}()")
    return "\n".join(lines)


def _extract_sql(raw: str) -> str:
    """Strip a ``` / ```sql fence if the model added one; the guardrail rejects
    anything still unparseable, so this only needs to handle the common wrap."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lower().startswith("sql"):
            text = text[3:]
    return text.strip()


def _to_results(rows: list[dict[str, Any]], table: str) -> tuple[tuple[RetrievalResult, ...], int]:
    """One source row → one §16 ``row`` result cited by ``(table, pk)``. A row with
    no usable pk is dropped (uncitable, §27.2), not emitted — returns the ordered
    results AND the count dropped, so the caller can surface the shortfall (§22: an
    incomplete answer must carry a warning, not look complete). A missing/non-string
    ``pk`` means a corrupt or hand-written ``documents`` row; C2's connector always
    writes one, so ``dropped`` is 0 on well-formed builds."""
    results: list[RetrievalResult] = []
    total = len(rows)
    dropped = 0
    for index, row in enumerate(rows):
        pk = row.get("__row_pk")
        if not isinstance(pk, str) or not pk:
            dropped += 1
            continue
        source_uri = row.get("__source_uri")
        data = {key: value for key, value in row.items() if not key.startswith("__")}
        ref = SourceRef(
            source_type="row",
            id=row_source_ref(table, pk),
            source_uri=source_uri if isinstance(source_uri, str) else None,
            metadata={"table": table, "pk": pk},
        )
        results.append(
            RetrievalResult(
                result_type="row",
                id=row_source_ref(table, pk),
                # positional score: no relevance ranking for SQL, but a strictly
                # descending value keeps the query's own ORDER BY through
                # ordered_results (score desc); confidence stays None
                score=(total - index) / total,
                source_refs=(ref,),
                text=json.dumps(data, ensure_ascii=False, sort_keys=True),
            )
        )
    return ordered_results(results), dropped


#: Postgres SQLSTATE for a statement cancelled by ``statement_timeout``
#: (query_canceled) — driver-agnostic (exposed on the DB-API ``orig``), so we
#: don't couple this layer to asyncpg's exception classes.
_QUERY_CANCELED_SQLSTATE = "57014"


def _is_timeout(exc: DBAPIError) -> bool:
    """True when the DB cancelled the statement for exceeding the deadline."""
    return getattr(exc.orig, "sqlstate", None) == _QUERY_CANCELED_SQLSTATE


def _warn(code: str, message: str) -> QueryWarning:
    return QueryWarning(code, message)


def _response(
    reader: BuildScopedSqlReader,
    query: str,
    results: tuple[RetrievalResult, ...],
    warnings: tuple[QueryWarning, ...],
    debug: dict[str, Any] | None = None,
) -> McpResponse:
    return McpResponse(
        query=query,
        tool=_TOOL,
        project=reader.project,
        build_id=str(reader.build_id),
        results=results,
        warnings=warnings,
        debug=debug,
    )


def _debug(
    expose: bool,
    started: float,
    *,
    sql: str | None,
    note: str,
    reached_store: bool = True,
) -> dict[str, Any] | None:
    """The §16 debug block for one sql call, or None when not authorised.

    ``retrieval_plan`` carries the GENERATED SQL — the field hybrid already
    uses for "what the retrieval actually did", and the only place a caller can
    learn what their question became. ``None`` sql means the run ended before
    one existed (mode disabled, schema discovery failed), which is itself the
    answer to "what ran".

    Shape matches the frozen ``Debug`` (§16: stores_used · retrieval_plan ·
    routing_decision · latency_ms, additive evolution). ``routing_decision``
    is NULL because the schema says so for single-mode tools that bypass the
    router, and ``reached_store`` is False on the one path that answers
    without touching Postgres at all.
    """
    if not expose:
        return None
    return {
        # ONLY what was actually read. The mode-disabled path reaches no store,
        # and a provenance block whose whole argument is honesty must not claim
        # one — the rule hybrid already keeps by computing this from COMPLETED
        # modes only.
        "stores_used": ["postgres"] if reached_store else [],
        "retrieval_plan": [f"sql: {note}"] + ([f"generated: {sql}"] if sql else []),
        # NULL by contract, not by omission: the frozen schema says "Null for
        # single-mode tools that bypass the router", and there is no router
        # here to trace. Fabricating one is also user-visible — the Console
        # renders routing_decision and nothing else of this block, so a made-up
        # trace would be the ONE part of it a person sees.
        "routing_decision": None,
        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
    }
