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
) -> McpResponse:
    """§8 sql retrieval over the active build, as a §16 response.

    ``reader`` is bound to the active build (DR-001); ``policy`` is the resolved
    ``text_to_sql`` block; ``max_rows`` is the caller-reconciled row ceiling
    (``min`` of the top-level ``max_sql_rows`` and ``text_to_sql.max_rows``).
    """
    if not policy.enabled:
        return _response(reader, query, (), (_warn("MODE_SKIPPED", "sql mode is disabled"),))

    schema = await _schema_prompt(reader, policy.allowed_tables)
    candidate = _extract_sql(await _ask_llm(llm, schema, query))

    try:
        validated = validate_sql(candidate, policy.allowed_tables, policy.blocked_keywords)
    except GuardrailBlocked as blocked:
        return _response(reader, query, (), (_warn(GUARDRAIL_WARNING_CODE, blocked.reason),))

    try:
        rows, truncated = await reader.run(validated, max_rows, policy.timeout_ms)
    except DBAPIError as exc:
        # a guardrail-valid query the DB still rejects degrades (§22); a bug in
        # our own SQL composition is NOT a DBAPIError, so it fails loud (Rule 12).
        if _is_timeout(exc):
            # the policy deadline cancelled it — the answer is incomplete (§22
            # "逾時：回部分結果 + warning"), distinct from an invalid query.
            return _response(
                reader,
                query,
                (),
                (
                    _warn(
                        "PARTIAL_RESULTS",
                        f"query exceeded the {policy.timeout_ms}ms deadline (§21)",
                    ),
                ),
            )
        # otherwise an LLM-hallucinated column / bad cast — the query is unusable.
        return _response(
            reader,
            query,
            (),
            (
                _warn(
                    GUARDRAIL_WARNING_CODE,
                    f"the query could not be executed ({type(exc).__name__})",
                ),
            ),
        )

    results = _to_results(rows, validated.table)
    warnings: tuple[QueryWarning, ...] = ()
    if truncated:
        warnings = (_warn("TRUNCATED", f"result truncated to the {max_rows}-row ceiling (§21)"),)
    return _response(reader, query, results, warnings)


async def _ask_llm(llm: LLM, schema: str, query: str) -> str:
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=_SYSTEM),
        ChatMessage(role=MessageRole.USER, content=f"{schema}\n{_RULES}\nQuestion: {query}"),
    ]
    response = await llm.achat(messages)
    return response.message.content or ""


async def _schema_prompt(reader: BuildScopedSqlReader, allowed_tables: tuple[str, ...]) -> str:
    """List each whitelisted table with the columns it actually has in this
    build, so the LLM writes SQL against real column names."""
    lines = ["Tables (all columns are text):"]
    for table in allowed_tables:
        columns = await reader.column_names(table)
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


def _to_results(rows: list[dict[str, Any]], table: str) -> tuple[RetrievalResult, ...]:
    """One source row → one §16 ``row`` result cited by ``(table, pk)``. A row
    with no usable pk is dropped (uncitable), not emitted."""
    results: list[RetrievalResult] = []
    total = len(rows)
    for index, row in enumerate(rows):
        pk = row.get("__row_pk")
        if not isinstance(pk, str) or not pk:
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
    return ordered_results(results)


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
) -> McpResponse:
    return McpResponse(
        query=query,
        tool=_TOOL,
        project=reader.project,
        build_id=str(reader.build_id),
        results=results,
        warnings=warnings,
    )
