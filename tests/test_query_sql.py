"""Why: sql_query is the tool that turns an LLM's SQL into a §16 response, so it
owns the degrade-not-fail contract (§22) and the row citation (§27.2). These
tests pin that a disabled mode, a guardrail rejection, an execution error, and a
row-cap each come back as a TYPED warning over an empty/partial result — never a
raised exception — and that a real hit is a `row` result cited by (table, pk)
that validates against the frozen wire schema. The guardrail itself is real here
(only the reader + LLM are faked), so a bad LLM query is genuinely rejected.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
from llama_index.core.llms import LLM
from sqlalchemy.exc import OperationalError, ProgrammingError

from core.graph.structured import row_source_ref
from core.query.policy import (
    GUARDRAIL_WARNING_CODE,
    SQL_BLOCKED_KEYWORDS_MIN,
    TextToSql,
)
from core.query.results import McpResponse
from core.query.sql import sql_query
from core.stores.sqlreader import BuildScopedSqlReader

_BUILD = uuid.UUID("7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d")

_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "contracts" / "mcp_response.schema.json").read_text(
        encoding="utf-8"
    )
)
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


class _Canceled(Exception):
    """A DB-API `orig` carrying the query_canceled SQLSTATE, as asyncpg exposes
    when statement_timeout fires."""

    sqlstate = "57014"


class _FakeLLM:
    def __init__(self, sql: str) -> None:
        self._sql = sql

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(message=SimpleNamespace(content=self._sql))


class _FakeReader:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        truncated: bool = False,
        columns: tuple[str, ...] = ("id", "amount"),
        raise_run: bool = False,
        raise_bug: bool = False,
        raise_timeout: bool = False,
        raise_discovery: bool = False,
        raise_guardrail: bool = False,
    ) -> None:
        self.project = "acme"
        self.build_id = _BUILD
        self._rows = rows or []
        self._truncated = truncated
        self._columns = columns
        self._raise = raise_run
        self._bug = raise_bug
        self._timeout = raise_timeout
        self._discovery = raise_discovery
        self._guardrail = raise_guardrail
        self.timeout_ms: int | None = None
        self.rolled_back = False

    @asynccontextmanager
    async def timed_transaction(self, timeout_ms: int) -> AsyncIterator[None]:
        self.timeout_ms = timeout_ms  # capture the deadline the tool plumbed through
        try:
            yield
        finally:
            self.rolled_back = True  # each phase's transaction is ended on exit

    async def columns_by_table(self, tables: Any) -> dict[str, tuple[str, ...]]:
        if self._discovery:
            # a schema-discovery scan cancelled by statement_timeout (SQLSTATE 57014)
            raise OperationalError("SELECT jsonb_object_keys ...", {}, _Canceled())
        return {t: self._columns for t in tables}

    async def run(self, validated: Any, max_rows: int) -> tuple[list[dict[str, Any]], bool]:
        if self._bug:
            raise RuntimeError("a bug in SQL composition")
        if self._guardrail:
            # run() refuses a query it can't execute faithfully (e.g. a data column
            # name past the identifier limit) — a typed guardrail rejection.
            from core.query.sql_guard import GuardrailBlocked

            raise GuardrailBlocked("a structured column name exceeds the identifier limit")
        if self._timeout:
            # a statement cancelled by statement_timeout (SQLSTATE 57014)
            raise OperationalError("SELECT ...", {}, _Canceled())
        if self._raise:
            # the DB rejecting a guardrail-valid query (unknown column / bad cast)
            raise ProgrammingError("SELECT ...", {}, Exception("column does not exist"))
        return list(self._rows), self._truncated


def _run(
    reader: _FakeReader,
    llm: _FakeLLM,
    policy: TextToSql = _POLICY,
    *,
    expose_debug: bool = False,
) -> McpResponse:
    import asyncio

    return asyncio.run(
        sql_query(
            cast(BuildScopedSqlReader, reader),
            cast(LLM, llm),
            policy,
            "how many orders",
            expose_debug=expose_debug,
            max_rows=policy.max_rows,
        )
    )


def _codes(response: McpResponse) -> list[str]:
    return [w.code for w in response.warnings]


def test_disabled_mode_skips_with_a_typed_warning() -> None:
    """text_to_sql.enabled=false → MODE_SKIPPED, no LLM call, empty valid
    response — never an exception or a silently-empty answer."""
    disabled = TextToSql(
        enabled=False,
        allowed_tables=(),
        blocked_keywords=SQL_BLOCKED_KEYWORDS_MIN,
        max_rows=100,
        timeout_ms=5000,
    )
    reader = _FakeReader()
    response = _run(reader, _FakeLLM("SELECT * FROM orders"), disabled)
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["MODE_SKIPPED"]
    assert reader.rolled_back is False  # nothing ran → the connection is untouched


def test_a_blocked_query_degrades_to_guardrail_blocked() -> None:
    """The LLM emitting a write (the classic NL→SQL risk) is REJECTED by the real
    guardrail — GUARDRAIL_BLOCKED, empty results, never executed, never a 500."""
    reader = _FakeReader()
    response = _run(reader, _FakeLLM("DELETE FROM orders"))
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["GUARDRAIL_BLOCKED"]
    assert reader.rolled_back is True  # the SET LOCAL is rolled back too (no deadline leak)


def test_a_column_alias_attack_is_blocked_before_it_can_forge_a_citation() -> None:
    """An LLM emitting a column-alias list that would rename a data column onto
    __row_pk is REJECTED by the (real) guardrail — GUARDRAIL_BLOCKED, the query is
    never executed — so a forged (table, pk) can never reach _to_results (§27.2)."""
    reader = _FakeReader(rows=[{"__row_pk": "forged", "id": "1"}])
    response = _run(reader, _FakeLLM("SELECT * FROM orders AS o(id, __row_pk)"))
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["GUARDRAIL_BLOCKED"]


def test_a_hit_is_a_row_result_cited_by_table_and_pk() -> None:
    """A matched source row becomes a §16 `row` result: id + source_ref by
    (table, pk), the row's columns as text, validating against the frozen
    schema — the §27.2 row minimum met end to end."""
    reader = _FakeReader(
        rows=[{"__row_pk": "7", "__source_uri": "s3://orders.csv#id=7", "id": "7", "amount": "9"}]
    )
    response = _run(reader, _FakeLLM("SELECT * FROM orders WHERE amount::numeric > 5"))
    payload = response.to_dict()
    _VALIDATOR.validate(payload)
    assert response.warnings == ()
    (result,) = payload["results"]
    assert result["result_type"] == "row"
    assert result["id"] == row_source_ref("orders", "7")
    (ref,) = result["source_refs"]
    assert ref["source_type"] == "row"
    assert ref["metadata"] == {"table": "orders", "pk": "7"}
    assert ref["source_uri"] == "s3://orders.csv#id=7"
    assert json.loads(result["text"]) == {"id": "7", "amount": "9"}  # __-prefixed cols stripped


def test_a_row_with_no_pk_is_dropped_and_surfaced_as_partial() -> None:
    """A row missing a usable pk can't be cited (§27.2), so it is dropped rather
    than emitted uncited — the read/emit discipline applied to SQL rows. And the
    drop is SURFACED as PARTIAL_RESULTS (§22): a short answer must not look
    complete when rows were silently omitted."""
    reader = _FakeReader(
        rows=[
            {"__row_pk": None, "id": "1"},  # no pk → dropped
            {"__row_pk": "2", "__source_uri": "s3://x", "id": "2"},
        ]
    )
    response = _run(reader, _FakeLLM("SELECT * FROM orders"))
    _VALIDATOR.validate(response.to_dict())
    assert [r.source_refs[0].metadata["pk"] for r in response.results] == ["2"]
    assert _codes(response) == ["PARTIAL_RESULTS"]  # the omitted row is not hidden


def test_truncation_is_surfaced() -> None:
    """The max_rows ceiling clipping the result set is a TRUNCATED warning
    alongside the (partial) results, not a silent cut."""
    reader = _FakeReader(
        rows=[{"__row_pk": "1", "__source_uri": "s3://x", "id": "1"}], truncated=True
    )
    response = _run(reader, _FakeLLM("SELECT * FROM orders"))
    _VALIDATOR.validate(response.to_dict())
    assert _codes(response) == ["TRUNCATED"] and len(response.results) == 1


def test_an_execution_error_degrades_not_raises() -> None:
    """A guardrail-valid query that still fails at execution (unknown column, bad
    cast) degrades to GUARDRAIL_BLOCKED — the query doesn't 500 the caller."""
    reader = _FakeReader(raise_run=True)
    response = _run(reader, _FakeLLM("SELECT * FROM orders WHERE nope = '1'"))
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["GUARDRAIL_BLOCKED"]


def test_a_successful_query_ends_each_phase_transaction() -> None:
    """Schema discovery and execution each run in their OWN short timed transaction,
    ended on exit — so the SET LOCAL statement_timeout never leaks to a reused
    connection and nothing is held across the LLM call between the phases (the
    integration test proves the connection is not in a transaction during the LLM)."""
    reader = _FakeReader(rows=[{"__row_pk": "1", "__source_uri": "s3://x", "id": "1"}])
    response = _run(reader, _FakeLLM("SELECT * FROM orders"))
    assert response.warnings == () and reader.rolled_back is True
    assert reader.timeout_ms == _POLICY.timeout_ms  # the deadline was plumbed into each phase


def test_a_run_time_guardrail_rejection_degrades_to_guardrail_blocked() -> None:
    """run() can refuse a query it can't execute FAITHFULLY (e.g. a data column name
    past PostgreSQL's identifier limit); sql_query surfaces that as GUARDRAIL_BLOCKED
    over an empty result — a typed rejection, never a 500 — and the phase txn ends."""
    reader = _FakeReader(raise_guardrail=True)
    response = _run(reader, _FakeLLM("SELECT * FROM orders"))
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["GUARDRAIL_BLOCKED"]
    assert reader.rolled_back is True  # the phase transaction still ends


def test_a_timeout_degrades_to_partial_results() -> None:
    """A statement cancelled at the policy deadline (§21 timeout_ms) is the §22
    timeout degradation — PARTIAL_RESULTS (the answer is incomplete), distinct
    from GUARDRAIL_BLOCKED (the query was invalid) — and the tool passes the
    policy's timeout through and rolls the aborted transaction back."""
    reader = _FakeReader(raise_timeout=True)
    response = _run(reader, _FakeLLM("SELECT * FROM orders"))  # valid; the reader fakes the cancel
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["PARTIAL_RESULTS"]
    assert reader.timeout_ms == _POLICY.timeout_ms  # the deadline was actually plumbed through
    assert reader.rolled_back is True  # the aborted transaction is cleared before degrading


def test_a_schema_discovery_timeout_degrades_not_500s() -> None:
    """The deadline binds schema discovery too: a JSON-key scan over a large table
    that exceeds it degrades to PARTIAL_RESULTS (rolled back), never an unhandled
    500 before any guarded query runs — the timeout covers the whole path."""
    reader = _FakeReader(raise_discovery=True)
    response = _run(reader, _FakeLLM("SELECT * FROM orders"))
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["PARTIAL_RESULTS"]
    assert reader.rolled_back is True


def test_an_invalid_query_rolls_back_before_degrading() -> None:
    """A guardrail-valid query the DB rejects (unknown column) also leaves the
    transaction aborted — it is rolled back so a connection-reusing caller (the
    hybrid router) doesn't inherit a poisoned transaction (§22)."""
    reader = _FakeReader(raise_run=True)
    response = _run(reader, _FakeLLM("SELECT * FROM orders WHERE nope = '1'"))
    assert _codes(response) == ["GUARDRAIL_BLOCKED"] and reader.rolled_back is True


def test_a_code_bug_is_not_masked_as_a_warning() -> None:
    """Only DB-level query errors degrade — a bug in our own SQL composition (a
    non-DBAPI exception) must propagate and fail loud (Rule 12), not be laundered
    into a GUARDRAIL_BLOCKED warning that hides the defect."""
    import pytest

    reader = _FakeReader(raise_bug=True)
    with pytest.raises(RuntimeError):
        _run(reader, _FakeLLM("SELECT * FROM orders"))
    assert (
        reader.rolled_back is True
    )  # each phase's transaction still rolls back as the bug propagates


def test_a_fenced_llm_reply_is_unwrapped() -> None:
    """Models often wrap SQL in a ```sql fence; it is stripped before the
    guardrail so a well-formed query isn't rejected for the wrapper alone."""
    reader = _FakeReader(rows=[{"__row_pk": "1", "__source_uri": "s3://x", "id": "1"}])
    response = _run(reader, _FakeLLM("```sql\nSELECT * FROM orders\n```"))
    _VALIDATOR.validate(response.to_dict())
    assert len(response.results) == 1 and response.warnings == ()


def test_the_generated_sql_is_surfaced_when_debug_is_authorised() -> None:
    """An empty answer is unreadable without the SQL that produced it.

    The caller sends a QUESTION; the rows come from a machine translation of it
    they never see. So "0 rows" carries two completely different meanings — the
    data does not match, or the model understood something else — and nothing in
    the response distinguishes them. Every refusal path here already emits a
    typed warning, so the silent-empty case is precisely the one where NOTHING
    was refused and the caller has no other handle.

    This is provenance, not an answerability verdict: §19/MCP4 measured that one
    and refused it (out-of-domain questions outscored in-domain ones, so no
    threshold separates them). Saying WHAT RAN needs no such judgement.
    """
    reader = _FakeReader(rows=[])
    response = _run(reader, _FakeLLM("SELECT * FROM orders"), expose_debug=True)

    assert response.results == ()
    assert response.warnings == ()  # nothing was refused — that is the whole problem
    assert response.debug is not None
    plan = response.debug["retrieval_plan"]
    assert any("orders" in line for line in plan), plan
    assert response.debug["stores_used"] == ["postgres"]
    assert response.debug["latency_ms"] >= 0


def test_debug_is_withheld_unless_the_caller_is_authorised() -> None:
    """§16/§21 gate it — the SQL names real tables and columns, so it is not
    free to hand out. The DEFAULT must be closed: a caller that forgets to pass
    the flag gets nothing, rather than the schema leaking on every call."""
    reader = _FakeReader(rows=[])
    assert _run(reader, _FakeLLM("SELECT * FROM orders")).debug is None


def test_a_refused_query_still_shows_what_was_refused() -> None:
    """The guardrail case is where the SQL matters MOST: the caller gets a
    reason but, without this, no way to see the statement it applied to. The
    REFUSED candidate is what is shown — not a sanitised version — because the
    question 'what did my prompt turn into?' is the one being answered."""
    reader = _FakeReader(rows=[])
    response = _run(reader, _FakeLLM("DELETE FROM orders"), expose_debug=True)

    assert [w.code for w in response.warnings] == [GUARDRAIL_WARNING_CODE]
    assert response.debug is not None
    assert any("DELETE FROM orders" in line for line in response.debug["retrieval_plan"])


def test_a_disabled_mode_says_so_rather_than_showing_nothing() -> None:
    """No SQL exists on this path, and that IS the answer to "what ran" — the
    block still has to appear, or an authorised caller cannot tell a disabled
    mode from a mode that ran and found nothing."""
    disabled = replace(_POLICY, enabled=False)
    response = _run(_FakeReader(rows=[]), _FakeLLM("SELECT 1"), disabled, expose_debug=True)

    assert [w.code for w in response.warnings] == ["MODE_SKIPPED"]
    assert response.debug is not None
    assert any("disabled" in line for line in response.debug["retrieval_plan"])
    assert not any("generated:" in line for line in response.debug["retrieval_plan"])
