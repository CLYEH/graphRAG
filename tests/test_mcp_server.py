"""Why: the MCP server is the §9 facade — its TOOL VOCABULARY is frozen by
DESIGN (the eight names), an invalid policy must kill the server at BUILD
time (a guardrail misconfiguration must never serve queries half-armed), and
the demo project's shipped config must actually load. The tools' internals
are the C6 mode functions with their own suites; wiring is proven live in the
integration test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.mcp.policy import PolicyError
from core.mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parent.parent

#: §9's frozen tool set — adding/removing/renaming is a DESIGN change.
_FROZEN_TOOLS = {
    "semantic_search",
    "graph_query",
    "global_summary",
    "sql_query",
    "hybrid_query",
    "get_entity",
    "list_schema",
    "explain_retrieval",
}


async def test_the_server_exposes_exactly_the_frozen_tool_set() -> None:
    server = build_server("demo", REPO_ROOT / "projects" / "demo" / "config.yaml")
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == _FROZEN_TOOLS


def test_an_invalid_policy_kills_the_server_at_build_time(tmp_path: Path) -> None:
    """Fail loud at startup: a policy violating the frozen contract must stop
    the server from EXISTING, not surface later mid-query."""
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump({"query_policy": {"schema_version": "1.0"}}), "utf-8")
    with pytest.raises(PolicyError):
        build_server("broken", bad)


def test_the_shipped_demo_config_is_contract_valid() -> None:
    """The template a new project copies must itself pass the gate it
    documents — a broken example would teach broken configs."""
    server = build_server("demo", REPO_ROOT / "projects" / "demo" / "config.yaml")
    assert server.name == "graphrag-demo"


async def test_bounded_tools_degrade_typed_at_the_wall_clock_deadline() -> None:
    """§21: max_latency_ms bounds STANDALONE tools too, not just hybrid — a
    slow embedding/store call must come back as the typed §22 PARTIAL_RESULTS
    deadline degradation, never a hung MCP call. A fast runner passes through
    untouched (the over-block dual)."""
    import asyncio
    import uuid
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from typing import Any

    from core.mcp.server import _bounded, _Runtime
    from core.query.results import McpResponse

    build_id = uuid.uuid4()
    deps = SimpleNamespace(repo=SimpleNamespace(project="p", build_id=build_id))

    class _Ctx:
        project = "p"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            yield deps

    policy = SimpleNamespace(max_latency_ms=50)
    runtime = _Runtime(context=_Ctx(), policy=policy)  # type: ignore[arg-type]

    async def slow(_deps: Any, _remaining_ms: int) -> McpResponse:
        await asyncio.sleep(0.3)
        raise AssertionError("unreachable — the deadline must cancel first")

    payload = await _bounded(runtime, "semantic_search", "q", slow)
    assert payload["tool"] == "semantic_search"
    assert payload["build_id"] == str(build_id)
    assert payload["results"] == []
    assert payload["warnings"][0]["code"] == "PARTIAL_RESULTS"
    assert "deadline" in payload["warnings"][0]["message"]

    seen_budgets: list[int] = []

    async def fast(_deps: Any, remaining_ms: int) -> McpResponse:
        seen_budgets.append(remaining_ms)
        return McpResponse(
            query="q",
            tool="semantic_search",
            project="p",
            build_id=str(build_id),
            results=(),
            warnings=(),
        )

    ok = await _bounded(runtime, "semantic_search", "q", fast)
    assert ok["warnings"] == []  # a fast tool is untouched
    # the runner is handed what binding LEFT of the §21 budget — a pacer
    # inside it starts from the remainder, never a fresh full budget
    assert 0 < seen_budgets[0] <= 50

    class _StalledCtx:
        project = "p"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.3)  # binding itself stalls past the budget
            yield deps

    stalled = _Runtime(context=_StalledCtx(), policy=policy)  # type: ignore[arg-type]
    payload = await _bounded(stalled, "semantic_search", "q", fast)
    # the deadline covers BINDING too — no build resolved → nil-uuid sentinel
    assert payload["build_id"] == "00000000-0000-0000-0000-000000000000"
    assert "during scope binding" in payload["warnings"][0]["message"]


def test_the_introspection_timeout_shape_is_explicit() -> None:
    """The introspection tools are not §16 responses, so their §22 deadline
    degradation is an explicit error field — project/build_id/subject/error,
    never a hung call or a half-§16 hybrid shape."""
    import uuid
    from types import SimpleNamespace

    from core.mcp.server import _introspection_timeout, _Runtime

    build_id = uuid.uuid4()
    runtime = _Runtime(
        context=SimpleNamespace(project="p"),  # type: ignore[arg-type]
        policy=SimpleNamespace(max_latency_ms=1000),  # type: ignore[arg-type]
    )
    payload = _introspection_timeout(runtime, str(build_id), "list_schema")
    assert payload == {
        "project": "p",
        "build_id": str(build_id),
        "subject": "list_schema",
        "error": "query exceeded the 1000ms deadline (§21)",
    }
    # the deadline can fire DURING scope binding — no build was resolved,
    # and the nil-uuid sentinel + message detail say so honestly
    unbound = _introspection_timeout(runtime, None, "list_schema")
    assert unbound["build_id"] == "00000000-0000-0000-0000-000000000000"
    assert "during scope binding" in unbound["error"]


async def test_list_schema_maps_db_deadline_and_failures_typed() -> None:
    """Codex round-5: the STATEMENT deadline fires as a DB error (sqlstate
    57014), not asyncio.TimeoutError — uncaught it turned list_schema into an
    MCP error instead of the §22 shape. 57014 → the introspection timeout;
    any other DBAPIError → an explicit error naming the class; a non-DB bug
    still propagates LOUD (§22 degrades store trouble, never code bugs)."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from sqlalchemy.exc import DBAPIError

    from core.mcp.server import _list_schema, _Runtime

    class _PgTimeout(Exception):
        sqlstate = "57014"

    class _PgOther(Exception):
        sqlstate = "42P01"

    def _runtime(raising: BaseException) -> _Runtime:
        class _Reader:
            @asynccontextmanager
            async def timed_transaction(self, timeout_ms: int):  # type: ignore[no-untyped-def]
                raise raising
                yield

        deps = SimpleNamespace(
            repo=SimpleNamespace(build_id="b-1"),
            sql_reader=_Reader(),
        )

        class _Ctx:
            project = "p"

            @asynccontextmanager
            async def bound(self):  # type: ignore[no-untyped-def]
                yield deps

        policy = SimpleNamespace(
            max_latency_ms=1000,
            text_to_sql=SimpleNamespace(enabled=True, allowed_tables=("orders",)),
            sql_policy=lambda: SimpleNamespace(timeout_ms=500),
        )
        return _Runtime(context=_Ctx(), policy=policy)  # type: ignore[arg-type]

    timed_out = await _list_schema(_runtime(DBAPIError("q", None, _PgTimeout())))
    assert "deadline" in timed_out["error"]  # 57014 IS the §21 deadline

    failed = await _list_schema(_runtime(DBAPIError("q", None, _PgOther())))
    assert failed["error"] == "schema discovery failed (DBAPIError) — §22"
    assert failed["build_id"] == "b-1"

    with pytest.raises(ValueError, match="in-code bug"):
        await _list_schema(_runtime(ValueError("in-code bug")))
