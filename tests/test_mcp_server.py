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
