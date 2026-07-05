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
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
from llama_index.core.llms import LLM
from sqlalchemy.exc import ProgrammingError

from core.graph.structured import row_source_ref
from core.query.policy import SQL_BLOCKED_KEYWORDS_MIN, TextToSql
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
    ) -> None:
        self.project = "acme"
        self.build_id = _BUILD
        self._rows = rows or []
        self._truncated = truncated
        self._columns = columns
        self._raise = raise_run
        self._bug = raise_bug

    async def column_names(self, table: str) -> tuple[str, ...]:
        return self._columns

    async def run(self, validated: Any, max_rows: int) -> tuple[list[dict[str, Any]], bool]:
        if self._bug:
            raise RuntimeError("a bug in SQL composition")
        if self._raise:
            # the DB rejecting a guardrail-valid query (unknown column / bad cast)
            raise ProgrammingError("SELECT ...", {}, Exception("column does not exist"))
        return list(self._rows), self._truncated


def _run(reader: _FakeReader, llm: _FakeLLM, policy: TextToSql = _POLICY) -> McpResponse:
    import asyncio

    return asyncio.run(
        sql_query(
            cast(BuildScopedSqlReader, reader),
            cast(LLM, llm),
            policy,
            "how many orders",
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
    response = _run(_FakeReader(), _FakeLLM("SELECT * FROM orders"), disabled)
    _VALIDATOR.validate(response.to_dict())
    assert response.results == () and _codes(response) == ["MODE_SKIPPED"]


def test_a_blocked_query_degrades_to_guardrail_blocked() -> None:
    """The LLM emitting a write (the classic NL→SQL risk) is REJECTED by the real
    guardrail — GUARDRAIL_BLOCKED, empty results, never executed, never a 500."""
    response = _run(_FakeReader(), _FakeLLM("DELETE FROM orders"))
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


def test_a_row_with_no_pk_is_dropped_not_emitted_uncited() -> None:
    """A row missing a usable pk can't be cited (§27.2), so it is dropped rather
    than emitted uncited — the read/emit discipline applied to SQL rows."""
    reader = _FakeReader(
        rows=[
            {"__row_pk": None, "id": "1"},  # no pk → dropped
            {"__row_pk": "2", "__source_uri": "s3://x", "id": "2"},
        ]
    )
    response = _run(reader, _FakeLLM("SELECT * FROM orders"))
    _VALIDATOR.validate(response.to_dict())
    assert [r.source_refs[0].metadata["pk"] for r in response.results] == ["2"]


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


def test_a_code_bug_is_not_masked_as_a_warning() -> None:
    """Only DB-level query errors degrade — a bug in our own SQL composition (a
    non-DBAPI exception) must propagate and fail loud (Rule 12), not be laundered
    into a GUARDRAIL_BLOCKED warning that hides the defect."""
    import pytest

    with pytest.raises(RuntimeError):
        _run(_FakeReader(raise_bug=True), _FakeLLM("SELECT * FROM orders"))


def test_a_fenced_llm_reply_is_unwrapped() -> None:
    """Models often wrap SQL in a ```sql fence; it is stripped before the
    guardrail so a well-formed query isn't rejected for the wrapper alone."""
    reader = _FakeReader(rows=[{"__row_pk": "1", "__source_uri": "s3://x", "id": "1"}])
    response = _run(reader, _FakeLLM("```sql\nSELECT * FROM orders\n```"))
    _VALIDATOR.validate(response.to_dict())
    assert len(response.results) == 1 and response.warnings == ()
