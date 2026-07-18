"""Why: C8b's promise is that the §9 transport is RUN-time wiring — the tools
and policy must be byte-identical over HTTP and stdio, because the consuming
no-code agent platform (the museum-guide use case) speaks MCP over streamable
HTTP to exactly the server stdio serves locally. These tests pin the transport
vocabulary (a typo'd transport fails loud, never a silent stdio fallback that
strands the HTTP consumer), the host/port wiring from core.config (never
os.environ), and — hermetically, over the real ASGI app with the #37
factory-fake pattern (no API key, no stores, no sockets) — a genuine MCP
protocol round-trip: initialize + list_tools returns the full §9 tool set.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from core.config import get_settings
from core.mcp import server as server_module
from core.mcp.server import TRANSPORTS, build_server, run_server

REPO_ROOT = Path(__file__).resolve().parent.parent


# CFG1: policy rides the registry — hermetic tests stub the registry loader
# with the shared contract-valid fixture (conftest.DEMO_QUERY_POLICY)
def _stub_registry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.mcp.policy import query_policy_from_mapping
    from core.metadata.schema import MetadataExposure
    from tests.conftest import DEMO_QUERY_POLICY

    policy = query_policy_from_mapping(DEMO_QUERY_POLICY)

    async def fake_load(engine: object, project: str) -> object:
        return policy, MetadataExposure(fields=())

    monkeypatch.setattr(server_module, "_load_runtime_config", fake_load)


#: the frozen §9 tool set every transport must expose identically
_TOOLS = {
    "semantic_search",
    "graph_query",
    "global_summary",
    "sql_query",
    "hybrid_query",
    "get_entity",
    "list_schema",
    "explain_retrieval",
}


def test_transport_vocabulary_is_stdio_and_http_only() -> None:
    # §9 marks transport 🔧 stdio/http — "http" maps to the MCP spec's current
    # streamable HTTP; the SDK's legacy SSE flavor is deliberately not offered
    # (one HTTP transport, no ambiguity for the consuming platform)
    assert TRANSPORTS == {"stdio": "stdio", "http": "streamable-http"}


def test_run_server_maps_the_vocabulary_and_fails_loud() -> None:
    calls: list[str] = []
    fake = SimpleNamespace(run=lambda transport: calls.append(transport))

    run_server(fake, "stdio")  # type: ignore[arg-type]
    run_server(fake, "http")  # type: ignore[arg-type]
    assert calls == ["stdio", "streamable-http"]

    # WHY: a typo'd transport must never silently fall back to stdio — the
    # HTTP consumer would wait on a port nothing is bound to
    with pytest.raises(ValueError, match="unknown transport"):
        run_server(fake, "sse")  # type: ignore[arg-type]
    assert calls == ["stdio", "streamable-http"]  # nothing ran


def test_http_binding_comes_from_core_config() -> None:
    # host/port ride core.config (never os.environ) into the FastMCP settings
    # the streamable-http runner binds
    server = build_server("demo")
    assert server.settings.host == get_settings().mcp_http_host
    assert server.settings.port == get_settings().mcp_http_port


async def test_streamable_http_serves_the_full_tool_set_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: the transport-additivity promise, protocol-level — a real MCP
    # initialize + list_tools over the streamable-HTTP ASGI app must expose
    # exactly the §9 tool set, with the lifespan's store/model factories faked
    # (#37 lesson: construction must not require an API key or live stores;
    # initialize/list_tools invoke no tool, so fakes are never called through).
    class _Closeable:
        async def aclose(self) -> None:  # ProjectContext.aclose closes these
            return None

        async def close(self) -> None:
            return None

        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(server_module, "create_async_engine", lambda *a, **k: _Closeable())
    monkeypatch.setattr(server_module, "vector_client", lambda: _Closeable())
    monkeypatch.setattr(server_module, "graph_driver", lambda: _Closeable())
    monkeypatch.setattr(server_module, "embedding_model", lambda: object())
    monkeypatch.setattr(server_module, "chat_model", lambda: object())
    _stub_registry_policy(monkeypatch)

    server = build_server("demo")
    app = server.streamable_http_app()
    # with the localhost default, the SDK auto-enables DNS-rebinding
    # protection whose Host allowlist admits only the configured binding — any
    # other authority is a 421, so the in-process client must use exactly
    # core.config's host:port. NOTE a NON-localhost host disables the SDK's
    # rebinding protection entirely (no allowlist is injected) — binding wider
    # drops this layer too, making §23 auth the real gate.
    base = f"http://{server.settings.host}:{server.settings.port}"

    # ASGITransport does not drive lifespan — enter it explicitly (the BA2e-2
    # lesson); this starts the session manager AND the server lifespan (which
    # builds the faked context above)
    http_client = httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url=base,
        # generous read timeout: the transport holds an SSE stream open
        timeout=httpx.Timeout(30, read=300),
    )
    async with (
        app.router.lifespan_context(app),
        streamable_http_client(f"{base}/mcp", http_client=http_client) as (
            read,
            write,
            _get_session_id,
        ),
        ClientSession(read, write) as session,
    ):
        result = await session.initialize()
        assert result.serverInfo.name == "graphrag-demo"
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == _TOOLS


async def test_http_sessions_get_isolated_runtimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (Codex #58 P1): streamable HTTP multiplexes MANY protocol sessions
    # on one FastMCP instance, and the SDK enters the lifespan once PER
    # session — a module-level runtime slot would be overwritten by every
    # later session and, once any session closed, hand the survivors closed
    # store clients. Each session must see ITS OWN lifespan runtime, and one
    # session's shutdown must close only its own stores.
    disposed: list[int] = []

    class _Engine:
        async def dispose(self) -> None:
            disposed.append(id(self))

    class _Store:
        async def close(self) -> None:
            return None

    monkeypatch.setattr(server_module, "create_async_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(server_module, "vector_client", lambda: _Store())
    monkeypatch.setattr(server_module, "graph_driver", lambda: _Store())
    monkeypatch.setattr(server_module, "embedding_model", lambda: object())
    monkeypatch.setattr(server_module, "chat_model", lambda: object())
    _stub_registry_policy(monkeypatch)

    server = build_server("demo")

    @server.tool()
    async def runtime_probe() -> str:  # test-only: which runtime does this session see?
        return str(id(server.get_context().request_context.lifespan_context))

    app = server.streamable_http_app()
    base = f"http://{server.settings.host}:{server.settings.port}"

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base,
            timeout=httpx.Timeout(30, read=300),
        )

    async def _probe(session: ClientSession) -> str:
        result = await session.call_tool("runtime_probe", {})
        return result.content[0].text  # type: ignore[union-attr]

    async with app.router.lifespan_context(app):
        async with AsyncExitStack() as stack:
            read_a, write_a, _ = await stack.enter_async_context(
                streamable_http_client(f"{base}/mcp", http_client=_client())
            )
            session_a = await stack.enter_async_context(ClientSession(read_a, write_a))
            await session_a.initialize()
            a_runtime = await _probe(session_a)

            # a SECOND session initializes: its lifespan must not displace A's
            async with AsyncExitStack() as b_stack:
                read_b, write_b, _ = await b_stack.enter_async_context(
                    streamable_http_client(f"{base}/mcp", http_client=_client())
                )
                session_b = await b_stack.enter_async_context(ClientSession(read_b, write_b))
                await session_b.initialize()
                b_runtime = await _probe(session_b)
                assert b_runtime != a_runtime  # each session sees its OWN runtime

            # B closed: only B's stores may be disposed, and A still resolves
            # to the SAME runtime it started with
            assert await _probe(session_a) == a_runtime
        assert len(disposed) >= 1  # B's engine went down with B
    assert len(disposed) == 2  # ...and A's only when A closed
