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

    async def slow(_deps: Any) -> McpResponse:
        await asyncio.sleep(0.3)
        raise AssertionError("unreachable — the deadline must cancel first")

    payload = await _bounded(runtime, "semantic_search", "q", slow)
    assert payload["tool"] == "semantic_search"
    assert payload["build_id"] == str(build_id)
    assert payload["results"] == []
    assert payload["warnings"][0]["code"] == "PARTIAL_RESULTS"
    assert "deadline" in payload["warnings"][0]["message"]

    async def fast(_deps: Any) -> McpResponse:
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
    deps = SimpleNamespace(repo=SimpleNamespace(build_id=build_id))
    payload = _introspection_timeout(runtime, deps, "list_schema")
    assert payload == {
        "project": "p",
        "build_id": str(build_id),
        "subject": "list_schema",
        "error": "query exceeded the 1000ms deadline (§21)",
    }
