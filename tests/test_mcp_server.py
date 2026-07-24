"""Why: the MCP server is the §9 facade — its TOOL VOCABULARY is frozen by
DESIGN (the ten names), an invalid policy must kill the server at BUILD
time (a guardrail misconfiguration must never serve queries half-armed), and
the demo project's shipped config must actually load. The tools' internals
are the C6 mode functions with their own suites; wiring is proven live in the
integration test.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.mcp.policy import PolicyError
from core.mcp.server import _get_chunk, _get_document, build_server
from core.metadata.schema import MetadataExposure

#: DR-010's default: no metadata_exposure block → empty allowlist → nothing leaks
_NO_EXPOSURE = MetadataExposure(fields=())

REPO_ROOT = Path(__file__).resolve().parent.parent

#: §9's frozen tool set — adding/removing/renaming is a DESIGN change.
_FROZEN_TOOLS = {
    "semantic_search",
    "graph_query",
    "global_summary",
    "sql_query",
    "hybrid_query",
    "get_entity",
    "get_chunk",
    "get_document",
    "list_schema",
    "explain_retrieval",
}


async def test_the_server_exposes_exactly_the_frozen_tool_set() -> None:
    server = build_server("demo")
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == _FROZEN_TOOLS


async def test_registry_policy_failures_are_typed_and_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CFG1 moved the fail-loud gate from build time to SESSION start (the
    registry is read per lifespan): a missing project, a config without the
    policy block, and a contract-invalid block must each raise the typed
    PolicyError BEFORE the session serves a single query — never a
    half-configured server. The valid path returns the same policy the
    Console API validates (one SoR, shared validator by construction)."""
    from types import SimpleNamespace

    from core.mcp.policy import load_runtime_config_from_registry, query_policy_from_mapping
    from tests.conftest import DEMO_QUERY_POLICY

    rows: dict[str, object] = {}

    async def fake_get_project(conn: object, name: str) -> object | None:
        return rows.get(name)

    monkeypatch.setattr("core.registry.get_project", fake_get_project)

    with pytest.raises(PolicyError, match="not in the registry"):
        await load_runtime_config_from_registry(object(), "ghost")

    rows["bare"] = SimpleNamespace(config={})
    with pytest.raises(PolicyError, match="no query_policy block"):
        await load_runtime_config_from_registry(object(), "bare")

    rows["broken"] = SimpleNamespace(config={"query_policy": {"schema_version": "1.0"}})
    with pytest.raises(PolicyError):
        await load_runtime_config_from_registry(object(), "broken")

    rows["demo"] = SimpleNamespace(config={"query_policy": DEMO_QUERY_POLICY})
    policy, exposure = await load_runtime_config_from_registry(object(), "demo")
    assert policy == query_policy_from_mapping(DEMO_QUERY_POLICY)
    assert exposure.fields == ()  # no metadata_exposure block → fail-closed empty

    # #93 R2: a malformed metadata_exposure must not block a consumer that
    # never uses exposure (CLI eval) — the policy-only loader succeeds where
    # the composed loader (rightly) refuses
    from core.mcp.policy import load_query_policy_from_registry
    from core.metadata.schema import MetadataConfigError

    rows["mixed"] = SimpleNamespace(
        config={"query_policy": DEMO_QUERY_POLICY, "metadata_exposure": "not-a-mapping"}
    )
    assert await load_query_policy_from_registry(object(), "mixed") == policy
    with pytest.raises(MetadataConfigError):
        await load_runtime_config_from_registry(object(), "mixed")


async def test_bad_policy_error_is_not_masked_by_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #93 R5: the lifespan must read the registry policy BEFORE wiring
    any store/model client. When BOTH are broken (bad policy AND, say, no
    OPENAI_API_KEY), the operator must see the actionable PolicyError — a
    client factory that constructs first would mask it with its own error.
    Revert-probe: move the policy load back below ProjectContext(...) and this
    raises RuntimeError instead. The engine (the only client built pre-policy)
    must still be disposed — a failing session start must not leak pools."""
    from core.mcp import server as server_module

    disposed: list[bool] = []

    class _Engine:
        async def dispose(self) -> None:
            disposed.append(True)

    def _would_mask(_: object = None) -> object:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    async def _bad_policy(engine: object, project: str) -> object:
        raise PolicyError(f"project {project!r} has no query_policy block")

    monkeypatch.setattr(server_module, "create_async_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(server_module, "vector_client", _would_mask)
    monkeypatch.setattr(server_module, "graph_driver", _would_mask)
    monkeypatch.setattr(server_module, "embedding_model", _would_mask)
    monkeypatch.setattr(server_module, "chat_model", _would_mask)
    monkeypatch.setattr(server_module, "_load_runtime_config", _bad_policy)

    server = build_server("demo")
    assert server.settings.lifespan is not None
    with pytest.raises(PolicyError, match="no query_policy block"):
        async with server.settings.lifespan(server):
            pass  # pragma: no cover — startup must fail before the yield
    assert disposed == [True]  # the pre-policy engine was closed, not leaked


def test_the_demo_policy_fixture_is_contract_valid() -> None:
    """The shared test fixture every MCP test seeds (DEMO_QUERY_POLICY —
    successor of the deleted projects/demo/config.yaml template) must itself
    pass the frozen gate — a broken fixture would teach broken configs."""
    from core.mcp.policy import query_policy_from_mapping
    from tests.conftest import DEMO_QUERY_POLICY

    query_policy_from_mapping(DEMO_QUERY_POLICY)
    assert build_server("demo").name == "graphrag-demo"


async def test_bounded_tools_degrade_typed_at_the_wall_clock_deadline() -> None:
    """§21: max_latency_ms bounds STANDALONE tools too, not just hybrid — a
    slow embedding/store call must come back as the typed §22 PARTIAL_RESULTS
    deadline degradation, never a hung MCP call. A fast runner passes through
    untouched (the over-block dual)."""
    import asyncio
    import uuid
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

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
    # MCP2: the store is NAMED — "store unavailable" left the agent unable to
    # tell "route around Qdrant" from "everything is dead" (postgres down)
    assert failed["error"] == "postgres unavailable (DBAPIError) — §22"
    assert failed["build_id"] == "b-1"

    with pytest.raises(ValueError, match="in-code bug"):
        await _list_schema(_runtime(ValueError("in-code bug")))


async def test_store_outages_degrade_typed_but_code_bugs_stay_loud() -> None:
    """Codex round-8: a store exception during binding or the mode run (PG
    DBAPIError, Qdrant ApiException, Neo4j Neo4jError/DriverError) must come
    back as the §22 STORE_UNAVAILABLE typed response — never an MCP transport
    error. An in-code bug is NOT store trouble and still propagates loud."""
    import uuid
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import httpx
    from neo4j.exceptions import ServiceUnavailable
    from qdrant_client.http.exceptions import UnexpectedResponse
    from sqlalchemy.exc import OperationalError

    from core.mcp.server import _bounded, _Runtime

    build_id = uuid.uuid4()
    deps = SimpleNamespace(repo=SimpleNamespace(project="p", build_id=build_id))

    class _Ctx:
        project = "p"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            yield deps

    runtime = _Runtime(
        context=_Ctx(),  # type: ignore[arg-type]
        policy=SimpleNamespace(max_latency_ms=1000),  # type: ignore[arg-type]
    )

    outages = [
        OperationalError("q", None, Exception("pg down")),
        UnexpectedResponse(502, "bad gateway", b"", httpx.Headers()),
        ServiceUnavailable("neo4j down"),
    ]
    for outage in outages:

        async def _raise(_deps: Any, _remaining_ms: int) -> Any:
            raise outage  # noqa: B023 — bound per iteration on purpose

        payload = await _bounded(runtime, "semantic_search", "q", _raise)
        assert payload["results"] == []
        assert payload["warnings"][0]["code"] == "STORE_UNAVAILABLE"
        assert type(outage).__name__ in payload["warnings"][0]["message"]
        assert payload["build_id"] == str(build_id)  # bound before the outage

    class _DownCtx:
        project = "p"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            raise OperationalError("q", None, Exception("pg down"))
            yield deps

    down = _Runtime(
        context=_DownCtx(),  # type: ignore[arg-type]
        policy=SimpleNamespace(max_latency_ms=1000),  # type: ignore[arg-type]
    )

    async def _never(_deps: Any, _remaining_ms: int) -> Any:
        raise AssertionError("unreachable — binding failed first")

    payload = await _bounded(down, "semantic_search", "q", _never)
    assert payload["warnings"][0]["code"] == "STORE_UNAVAILABLE"
    assert payload["build_id"] == "00000000-0000-0000-0000-000000000000"  # never bound

    async def _bug(_deps: Any, _remaining_ms: int) -> Any:
        raise ValueError("in-code bug")

    with pytest.raises(ValueError, match="in-code bug"):
        await _bounded(runtime, "semantic_search", "q", _bug)


def test_active_binding_cannot_be_forged() -> None:
    """Codex round-9: bound_to taking a raw uuid made DR-001 caller
    discipline — any code could bind an archived build. The ActiveBinding
    proof restores the CONSTRUCTION fence: only resolve_active_binding()
    (the §27.1 lookup itself) can mint one; direct construction — with or
    without a guessed token — raises."""
    import uuid

    from core.stores.repo import ActiveBinding

    with pytest.raises(RuntimeError, match="resolve_active_binding"):
        ActiveBinding("p", uuid.uuid4())
    with pytest.raises(RuntimeError, match="resolve_active_binding"):
        ActiveBinding("p", uuid.uuid4(), object())  # guessed token

    # dataclasses.replace must not forge a REBOUND proof from a valid one:
    # the token is an InitVar (dropped by replace → falls back to None)
    import dataclasses

    import core.stores.repo as repo_module

    valid = ActiveBinding("p", uuid.uuid4(), repo_module._BINDING_TOKEN)
    with pytest.raises(RuntimeError, match="resolve_active_binding"):
        dataclasses.replace(valid, build_id=uuid.uuid4())


async def test_retrieval_tool_descriptions_state_score_semantics_honestly() -> None:
    """MCP4/DESIGN §22: v1 deliberately provides no out-of-domain signal —
    scores rank within a response and cannot flag an unanswerable question
    (measured: no separating threshold exists). The tool description is the
    ONLY surface an agent reads before calling, so the honesty statement
    lives there; this pin keeps a docstring rewrite from silently dropping
    the statement while the no-warning behavior stays."""
    server = build_server("demo")
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ("semantic_search", "hybrid_query"):
        assert "answerability from the returned content" in (tools[name].description or ""), name
    semantic = tools["semantic_search"].description or ""
    assert "no score threshold separates" in semantic
    # Codex #124: "read the text" is unfollowable on entity-only pages (text
    # is null there) — the description must say what a bare name-match page
    # means instead of pointing at a field that is empty exactly then
    assert "a page of bare name matches is NOT evidence" in semantic
    # ...and the replacement must not point at another dead end: get_entity
    # returns ids + mention refs only (no text), and ENTITY content stays
    # unreachable even after MCP5 — get_chunk takes chunk UUIDs, while an
    # entity mention ref is the chunk:{content_hash}:{ordinal} string shape
    # get_chunk deliberately rejects. REVERSE this pin when MCP7 makes
    # mention refs resolvable — the description should then point there.
    assert "get_entity" not in semantic
    assert "no tool currently retrieves" in semantic


class _IntrospectionRepo:
    """Fake BuildScopedRepo for the introspection helpers: canned rows, and a
    query log so tests can assert validation happens BEFORE any store read."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.build_id = uuid.uuid4()
        self.rows = rows or []
        self.queries = 0

    async def fetch_all(self, table: Any, *where: Any) -> list[Any]:
        self.queries += 1
        return self.rows


async def test_a_mention_ref_shaped_chunk_id_gets_a_typed_explanation() -> None:
    """MCP5/MCP7 seam: the OTHER chunk pointer an agent actually holds — an
    entity mention ref (chunk:{content_hash}:{ordinal}) — is NOT a chunk
    UUID, and a bare "invalid uuid" error would leave the agent with no idea
    why its perfectly-real-looking ref fails (the #124 lesson: name the gap,
    don't point at dead ends). The error must say that shape is not yet
    resolvable, and validation must not cost a store round-trip."""
    repo = _IntrospectionRepo()
    payload = await _get_chunk(repo, "demo", "chunk:3626c139ab:0")
    assert payload["chunk"] is None
    assert "not yet resolvable" in payload["error"]
    assert repo.queries == 0  # rejected before any store read

    document = await _get_document(repo, "demo", "not-a-uuid", _NO_EXPOSURE)
    assert document["document"] is None and "document UUID" in document["error"]
    assert repo.queries == 0


async def test_get_chunk_maps_the_row_and_types_not_found() -> None:
    """MCP5's whole point: a chunk UUID (relation evidence ref / chunk result
    id) must be exchangeable for the text it cites — before this tool the
    MCP surface had no citation→content path at all. Unknown ids get a typed
    not-found naming the ACTIVE build (never an exception: introspection
    degrades, §22)."""
    chunk_id, document_id = uuid.uuid4(), uuid.uuid4()
    row = SimpleNamespace(
        id=chunk_id,
        document_id=document_id,
        ordinal=3,
        text="全票 200 元",
        start_offset=10,
        end_offset=17,
        token_count=5,
    )
    payload = await _get_chunk(_IntrospectionRepo([row]), "demo", str(chunk_id))
    assert payload["error"] is None
    assert payload["chunk"]["text"] == "全票 200 元"
    assert payload["chunk"]["document_id"] == str(document_id)  # provenance rides along

    missing = await _get_chunk(_IntrospectionRepo(), "demo", str(uuid.uuid4()))
    assert missing["chunk"] is None and "ACTIVE build" in missing["error"]


async def test_get_document_emits_raw_whole_and_projects_metadata_fail_closed() -> None:
    """The document half: raw is emitted WHOLE (REST detail parity — silent
    truncation would misrepresent the corpus, §22), ingested_at is
    stringified (introspection payloads are plain JSON — no FastAPI encoder
    on this path), and metadata obeys DR-010 (Codex #125): the stored
    envelope is NOT agent-visible — it goes through the SAME fail-closed
    MetadataExposure projection as retrieval enrichment, so an unlisted
    governance field never leaks and an empty allowlist yields {}."""
    from datetime import UTC, datetime

    document_id = uuid.uuid4()
    row = SimpleNamespace(
        id=document_id,
        source_uri="file:///guide.md",
        mime="text/markdown",
        content_hash="abc123",
        metadata={"governance": {"classification": "secret"}, "context": {"title": "導覽"}},
        ingested_at=datetime(2026, 7, 24, tzinfo=UTC),
        status=None,
        raw="# 導覽 " + "全文" * 1000,
    )
    hidden = await _get_document(_IntrospectionRepo([row]), "demo", str(document_id), _NO_EXPOSURE)
    assert hidden["error"] is None
    doc = hidden["document"]
    assert doc["raw"] == row.raw  # whole, untruncated
    assert isinstance(doc["ingested_at"], str)
    assert doc["metadata"] == {}  # fail-closed: nothing allowlisted, NOTHING leaks

    listed = await _get_document(
        _IntrospectionRepo([row]),
        "demo",
        str(document_id),
        MetadataExposure(fields=("context.title",)),
    )
    assert listed["document"]["metadata"] == {"context": {"title": "導覽"}}  # only the listed path

    missing = await _get_document(_IntrospectionRepo(), "demo", str(uuid.uuid4()), _NO_EXPOSURE)
    assert missing["document"] is None and "ACTIVE build" in missing["error"]
