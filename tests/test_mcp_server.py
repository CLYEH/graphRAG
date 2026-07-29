"""Why: the MCP server is the §9 facade — its TOOL VOCABULARY is frozen by
DESIGN (the thirteen names), an invalid policy must kill the server at BUILD
time (a guardrail misconfiguration must never serve queries half-armed), and
the demo project's shipped config must actually load. The tools' internals
are the C6 mode functions with their own suites; wiring is proven live in the
integration test.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.mcp.policy import PolicyError
from core.mcp.server import (
    _get_chunk,
    _get_document,
    _incomplete_graph_invocation_payload,
    build_server,
)
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
    "list_entities",
    "list_chunks",
    "list_reports",
    "list_schema",
    "explain_retrieval",
}


async def test_the_server_exposes_exactly_the_frozen_tool_set() -> None:
    server = build_server("demo")
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == _FROZEN_TOOLS


async def test_tool_surface_metadata_is_complete() -> None:
    """MCP14 (measured tools/list): 19 parameters had NO description, the
    closed template vocabularies were bare strings without enum (an agent
    learned the legal values only from the rejection message), outputSchema
    was additionalProperties:true while the frozen response contract sat
    unattached, descriptions cited §-numbers an external agent cannot
    resolve, serverInfo.version reported the MCP SDK's version, and
    prompts/resources capabilities were declared with nothing behind them
    (two wasted round-trips for a spec-following agent). All metadata — the
    execution path is untouched (the http/integration suites prove calls
    still flow)."""
    import json

    from core.mcp.server import _SERVER_VERSION

    server = build_server("demo")
    tools = await server.list_tools()

    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert "§" not in tool.description, f"{tool.name} cites §-numbers agents cannot resolve"
        for pname, prop in tool.inputSchema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{pname} has no description"
            assert "§" not in prop["description"], f"{tool.name}.{pname} cites §-numbers"

    by_name = {tool.name: tool for tool in tools}
    closed = ["neighbors", "path", "subgraph"]
    # required param: the closed set alone; NULLABLE params include null in
    # the enum — pydantic emits anyOf[string,null] AND the enum applies to
    # both branches, so an explicit null must not fail the advertised schema
    # (Codex #134 r1)
    assert by_name["graph_query"].inputSchema["properties"]["template"]["enum"] == closed
    assert by_name["hybrid_query"].inputSchema["properties"]["graph_template"]["enum"] == [
        *closed,
        None,
    ]
    assert by_name["semantic_search"].inputSchema["properties"]["point_type"]["enum"] == [
        "chunk",
        "entity",
        None,
    ]

    # the six envelope-returning tools advertise the FROZEN contract —
    # minus its description annotations, which carry §/DR/change-history
    # jargon an external agent cannot resolve (Codex #134 r2); the
    # validation keywords must survive intact
    from core.mcp.server import _strip_schema_descriptions

    frozen = json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8"))
    sanitized = _strip_schema_descriptions(frozen)
    assert "§" not in json.dumps(sanitized) and "DR-0" not in json.dumps(sanitized)
    assert sanitized["properties"]["schema_version"]["const"] == "1.2"  # keywords intact
    assert sanitized["required"] == frozen["required"]
    for name in (
        "semantic_search",
        "graph_query",
        "global_summary",
        "sql_query",
        "hybrid_query",
        "explain_retrieval",
    ):
        assert by_name[name].outputSchema == sanitized, f"{name} outputSchema not the contract"

    # the server describes ITSELF: our version (not the SDK's), operating
    # instructions, a website, and NO empty prompts/resources capabilities
    assert server._mcp_server.version == _SERVER_VERSION  # noqa: SLF001
    assert server.instructions and "hybrid_query" in server.instructions
    from mcp.server.lowlevel.server import NotificationOptions

    caps = server._mcp_server.get_capabilities(  # noqa: SLF001
        notification_options=NotificationOptions(), experimental_capabilities={}
    )
    assert caps.prompts is None and caps.resources is None
    assert caps.tools is not None  # the real capability stays declared


def test_a_partial_graph_invocation_is_refused_not_silently_replanned() -> None:
    """MCP11: hybrid_query's docstring promises "run YOUR graph invocation",
    but a caller supplying only HALF of it (template without entity, or any
    optional refinement alone) used to be dropped silently — and the QP1
    auto plan would then run the router's OWN template/seed, so the agent
    believed THEIR invocation ran (measured: 票價 with explicit params → 0
    relations three times, no warning). The router must never trust its own
    guess over the caller's explicit instruction: every incomplete
    combination is a loud pre-binding GUARDRAIL_BLOCKED refusal with zero
    results that names what was supplied, what is missing, and that the
    caller's invocation did NOT run."""
    partial_cases: list[dict[str, Any]] = [
        {"graph_template": "neighbors"},
        {"graph_entity": "區域探索廳"},
        {"graph_other_entity": "潮境智能海洋館"},
        {"graph_hops": 2},
        {"graph_entity": "區域探索廳", "graph_hops": 2},
        {"graph_template": "path", "graph_other_entity": "潮境智能海洋館"},
    ]
    defaults: dict[str, Any] = {
        "graph_template": None,
        "graph_entity": None,
        "graph_other_entity": None,
        "graph_hops": None,
    }
    import json

    import jsonschema

    validator = jsonschema.Draft202012Validator(
        json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8")),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    for case in partial_cases:
        payload = _incomplete_graph_invocation_payload("demo", "票價", **{**defaults, **case})
        assert payload is not None, f"{case} must be refused"
        validator.validate(payload)  # the refusal is a contract-valid §16 envelope
        assert payload["results"] == []  # refused = nothing produced (n=0)
        assert payload["build_id"] == "00000000-0000-0000-0000-000000000000"  # pre-binding
        [warning] = payload["warnings"]
        assert warning["code"] == "GUARDRAIL_BLOCKED"
        for name in case:
            assert name in warning["message"]  # names what WAS supplied
        assert "did NOT run" in warning["message"]  # kills the "mine ran" belief
        missing = {"graph_template", "graph_entity"} - set(case)
        for name in missing:
            assert name in warning["message"]  # names what to add

    # COMPLETE invocations and a fully-absent one pass through (None = run):
    # template+entity is the contract pair; refinements may ride along; no
    # graph_* at all leaves QP1 free to plan.
    complete_cases: list[dict[str, Any]] = [
        {},
        {"graph_template": "neighbors", "graph_entity": "區域探索廳"},
        {
            "graph_template": "path",
            "graph_entity": "區域探索廳",
            "graph_other_entity": "潮境智能海洋館",
            "graph_hops": 2,
        },
    ]
    for case in complete_cases:
        assert _incomplete_graph_invocation_payload("demo", "票價", **{**defaults, **case}) is None


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
    monkeypatch.setattr(server_module, "query_embedding_model", _would_mask)
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


async def test_bounded_maps_every_expected_failure_family_typed() -> None:
    """MCP12 (§22): failure families that previously ESCAPED every retrieval
    tool as a raw MCP isError string (a Python exception repr an agent's
    JSON.parse chokes on) must each answer with a typed, contract-valid
    envelope. Measured escapes: a dead Postgres surfaces the builtin
    ConnectionRefusedError (not DBAPIError — "unexpected connection_lost()
    call" class); NoActiveBuildError is a LookupError; the model provider's
    OpenAIError family sits on the embedding/NL→SQL hot path where a single
    429 killed every tool — and its raw message (provider identity, token
    ceilings) must never be relayed to an untrusted caller. An oversized
    query is refused BEFORE binding (it would ride into provider token
    limits — a §21 refusal is actionable, a provider error is not)."""
    import json
    import uuid
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import jsonschema
    import openai

    from core.mcp.server import _bounded, _Runtime
    from core.query.results import McpResponse
    from core.stores.repo import NoActiveBuildError

    validator = jsonschema.Draft202012Validator(
        json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8")),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    build_id = uuid.uuid4()
    deps = SimpleNamespace(repo=SimpleNamespace(project="p", build_id=build_id))

    class _Ctx:
        project = "p"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            yield deps

    policy = SimpleNamespace(max_latency_ms=1000)
    runtime = _Runtime(context=_Ctx(), policy=policy)  # type: ignore[arg-type]

    async def _raising(exc: BaseException) -> Any:
        async def runner(_deps: Any, _remaining_ms: int) -> McpResponse:
            raise exc

        return await _bounded(runtime, "semantic_search", "q", runner)

    # a dead Postgres at connect: raw builtin ConnectionRefusedError
    payload = await _raising(ConnectionRefusedError("[Errno 111] refused"))
    validator.validate(payload)
    [warning] = payload["warnings"]
    assert warning["code"] == "STORE_UNAVAILABLE"
    assert "postgres" in warning["message"]

    # provider outage (429/network): typed degradation naming ONLY the class
    secret = "Rate limit reached for gpt-x in org-abc on tokens per min"
    payload = await _raising(openai.OpenAIError(secret))
    validator.validate(payload)
    [warning] = payload["warnings"]
    assert warning["code"] == "STORE_UNAVAILABLE"
    assert "model provider" in warning["message"]
    assert secret not in warning["message"]  # upstream text stays server-side

    # provider input-rejection (400/422): the caller's input, not an outage
    class _InputRejected(openai.OpenAIError):
        status_code = 400

    payload = await _raising(_InputRejected("bad request"))
    validator.validate(payload)
    assert payload["warnings"][0]["code"] == "GUARDRAIL_BLOCKED"

    # DR-001 lifecycle: no active build → the v1.2 typed warning, nil build
    class _NoBuildCtx:
        project = "p"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            raise NoActiveBuildError("p")
            yield deps

    no_build = _Runtime(context=_NoBuildCtx(), policy=policy)  # type: ignore[arg-type]

    async def _unreachable(_deps: Any, _remaining_ms: int) -> McpResponse:
        raise AssertionError("unreachable")

    payload = await _bounded(no_build, "semantic_search", "q", _unreachable)
    validator.validate(payload)
    [warning] = payload["warnings"]
    assert warning["code"] == "NO_ACTIVE_BUILD"
    assert "activate" in warning["message"]  # actionable, not a LookupError repr
    assert payload["build_id"] == "00000000-0000-0000-0000-000000000000"

    # oversized query: refused BEFORE the binding ever opens a store
    class _MustNotBind:
        project = "p"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            raise AssertionError("binding must not open for an over-cap query")
            yield deps

    capped = _Runtime(context=_MustNotBind(), policy=policy)  # type: ignore[arg-type]
    payload = await _bounded(capped, "semantic_search", "x" * 4001, _unreachable)
    validator.validate(payload)
    [warning] = payload["warnings"]
    assert warning["code"] == "GUARDRAIL_BLOCKED"
    assert "4000-char cap" in warning["message"]

    # in-code bugs still propagate LOUD — §22 degrades dependencies, not bugs
    with pytest.raises(ZeroDivisionError):
        await _raising(ZeroDivisionError())


def test_top_k_clamp_is_said_not_silent() -> None:
    """MCP13 (a): policy.top_k reconciles an over-cap ask via min() —
    silently. An agent asking 9999 and getting max_top_k results with EMPTY
    warnings cannot distinguish "the corpus only has this many" from "you
    were clamped" — the exact judgment (rephrase? page with list_*?) the
    warning informs; the OTHER end of the same parameter (negative top_k)
    already refuses loudly. The clamp must be SAID (TRUNCATED), and only
    when it actually happened (the over-block dual).

    QA4 moved the message itself beside the clamping method (core.mcp.policy)
    so the REST facade discloses the SAME clamp identically; this pins the MCP
    half of that shared contract."""
    from types import SimpleNamespace

    from core.mcp.policy import top_k_clamp_warning
    from core.mcp.server import _with_clamp_warning

    policy = SimpleNamespace(max_top_k=20)
    clamp = top_k_clamp_warning(policy, 9999)  # type: ignore[arg-type]
    assert clamp is not None
    assert clamp["code"] == "TRUNCATED"
    assert "9999" in clamp["message"] and "20" in clamp["message"]  # both numbers named

    # no ask / at-cap / under-cap: NOT clamped — no warning (over-block dual)
    for requested in (None, 20, 5):
        assert top_k_clamp_warning(policy, requested) is None  # type: ignore[arg-type]

    payload = {"build_id": "b-1", "warnings": [{"code": "MODE_SKIPPED", "message": "x"}]}
    out = _with_clamp_warning(payload, policy, 9999)  # type: ignore[arg-type]
    assert [w["code"] for w in out["warnings"]] == ["MODE_SKIPPED", "TRUNCATED"]

    # a nil-build payload is a pre-binding refusal / binding stall — the
    # retrieval never RAN, so claiming a clamp would overstate the response
    refused = {
        "build_id": "00000000-0000-0000-0000-000000000000",
        "warnings": [{"code": "GUARDRAIL_BLOCKED", "message": "refused"}],
    }
    out = _with_clamp_warning(refused, policy, 9999)  # type: ignore[arg-type]
    assert [w["code"] for w in out["warnings"]] == ["GUARDRAIL_BLOCKED"]


def test_explain_retrieval_refusal_is_a_real_refusal() -> None:
    """MCP13 (b): GUARDRAIL_BLOCKED means "refused, nothing produced"
    everywhere else — but explain_retrieval used to run the FULL pipeline
    (5.4s + real LLM spend), return a successful page, and stamp it
    GUARDRAIL_BLOCKED: an agent applying the uniform rule discards a good
    answer. The refusal shape must be a true refusal: zero results, nil
    build (nothing ran), one code one meaning — and contract-valid."""
    import json

    import jsonschema

    from core.mcp.server import _debug_disabled_payload, _unusable_query_payload

    payload = _debug_disabled_payload("demo", "票價")
    validator = jsonschema.Draft202012Validator(
        json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8")),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    validator.validate(payload)
    assert payload["results"] == []  # NOTHING was produced — the code's one meaning
    assert payload["build_id"] == "00000000-0000-0000-0000-000000000000"  # never ran
    [warning] = payload["warnings"]
    assert warning["code"] == "GUARDRAIL_BLOCKED"
    assert "refused" in warning["message"] and "hybrid_query" in warning["message"]

    # Codex #133 r1: the debug refusal echoes the query, and the tool's
    # early return bypasses _bounded's cap — so the SHARED cap helper must
    # run first, truncating the echo (no whole-input reflection/amplification)
    oversized = _unusable_query_payload("demo", "hybrid_query", "x" * 4001)
    assert oversized is not None
    validator.validate(oversized)
    assert len(oversized["query"]) == 200  # echo truncated, never reflected whole
    assert "4000-char cap" in oversized["warnings"][0]["message"]
    assert _unusable_query_payload("demo", "hybrid_query", "x" * 4000) is None  # dual

    # Codex #140 r5: the O(1) length check runs BEFORE the per-character
    # storability/blankness scan. Pinned by DIAGNOSIS, which discriminates the
    # order without a timing assertion: an oversized query that ALSO carries a
    # NUL is refused on LENGTH — its content is never scanned, so the message
    # names the cap, not the NUL. Scan-first would have named the NUL instead.
    oversized_and_bad = "\x00" + "x" * 4001
    both = _unusable_query_payload("demo", "hybrid_query", oversized_and_bad)
    assert both is not None
    msg = both["warnings"][0]["message"]
    assert "4000-char cap" in msg and "NUL" not in msg  # length decided it, content unseen
    # and an oversized query carrying a SURROGATE still serializes: its echo
    # runs through _safe_echo, so length-first cannot smuggle a lone surrogate
    # into the refusal and kill the response (D11 through the oversized path)
    surrogate_oversized = _unusable_query_payload("demo", "hybrid_query", "\ud800" + "x" * 4001)
    assert surrogate_oversized is not None
    json.dumps(surrogate_oversized, ensure_ascii=False).encode("utf-8")

    # gate-2 sweep: hybrid_query's MCP11 incomplete-invocation refusal is the
    # SAME pre-_bounded early-return class — the tool runs the cap first,
    # AND (defense in depth) the refusal itself truncates its echo, so no
    # ordering regression can ever reflect a large input whole.
    from core.mcp.server import _incomplete_graph_invocation_payload as _partial

    reflected = _partial(
        "demo",
        "x" * 4001,
        graph_template=None,
        graph_entity="區域探索廳",
        graph_other_entity=None,
        graph_hops=None,
    )
    assert reflected is not None
    validator.validate(reflected)
    assert len(reflected["query"]) == 200  # never reflected whole, even helper-direct

    # symmetric hardening: the debug refusal truncates its echo too
    assert len(_debug_disabled_payload("demo", "x" * 4001)["query"]) == 200

    # Codex #133 r2 dual: a WITHIN-cap query is echoed WHOLE — clients
    # correlate/log/retry from the §16 envelope, and truncating a legal
    # 201..4000-char query would silently corrupt that correlation
    legal = "y" * 300
    within = _partial(
        "demo",
        legal,
        graph_template=None,
        graph_entity="區域探索廳",
        graph_other_entity=None,
        graph_hops=None,
    )
    assert within is not None and within["query"] == legal
    assert _debug_disabled_payload("demo", legal)["query"] == legal


async def test_introspection_errors_carry_typed_codes() -> None:
    """MCP13 (c): the introspection tools' free-text error field squashed
    input-error / timeout / store-outage into one untyped string — three
    states whose correct agent reactions differ completely (fix the input /
    retry later / back off). Every error shape now carries a typed
    error_code sibling; successes carry None."""
    import uuid
    from types import SimpleNamespace

    from core.mcp.server import (
        _get_chunk,
        _introspection_no_active_build,
        _introspection_store_error,
        _introspection_timeout,
        _invalid_document_payload,
        _list_entities,
        _Runtime,
    )

    runtime = _Runtime(
        context=SimpleNamespace(project="p"),  # type: ignore[arg-type]
        policy=SimpleNamespace(max_latency_ms=1000),  # type: ignore[arg-type]
    )
    assert _introspection_timeout(runtime, None, "s")["error_code"] == "QUERY_TIMEOUT"
    assert (
        _introspection_store_error(runtime, None, "s", ConnectionRefusedError())["error_code"]
        == "STORE_UNAVAILABLE"
    )
    assert _introspection_no_active_build(runtime, "s")["error_code"] == "NO_ACTIVE_BUILD"
    invalid = _invalid_document_payload("p", "not-a-uuid")
    assert invalid is not None and invalid["error_code"] == "INVALID_INPUT"

    # browse input refusal (pre-store) and NOT_FOUND both typed
    repo = SimpleNamespace(build_id=uuid.uuid4())
    page = await _list_entities(repo, "p", 0, None, None, None)  # limit 0 = invalid
    assert page["error_code"] == "INVALID_INPUT"

    async def _fetch_all(table: Any, where: Any) -> list[Any]:
        return []

    repo_empty = SimpleNamespace(build_id=uuid.uuid4(), fetch_all=_fetch_all)
    missing = await _get_chunk(repo_empty, "p", str(uuid.uuid4()))
    assert missing["error_code"] == "NOT_FOUND"


async def test_list_schema_discloses_the_session_policy_ceilings() -> None:
    """MCP15: of the ten operative limits only the SQL whitelist was
    discoverable up front — max_top_k/max_graph_hops/max_latency_ms/
    expose_debug/enabled modes/query caps could be learned only by tripping
    them, and PER-PROJECT policy divergence was invisible (measured: an
    operator's max_top_k=3 edit changed behavior with no discoverable
    surface reflecting it). list_schema now discloses the session's ACTUAL
    policy — the divergent values below are deliberately non-default."""
    import uuid
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from core.mcp.server import _list_schema, _Runtime

    build_id = uuid.uuid4()
    deps = SimpleNamespace(repo=SimpleNamespace(project="museum", build_id=build_id))

    class _Ctx:
        project = "museum"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            yield deps

    policy = SimpleNamespace(
        default_mode="semantic",
        max_top_k=3,  # the measured divergent edit
        max_graph_hops=2,
        max_sql_rows=50,
        max_latency_ms=9000,
        expose_debug=True,
        text_to_sql=SimpleNamespace(enabled=False),
        sql_rows=lambda: 40,  # the RECONCILED cap (min of top-level and mode-local)
        # mode-local reconciled ceilings (Codex #135 r2): rows below top_k,
        # timeouts below max_latency_ms — the divergences that would be
        # overstated by raw top-level fields
        sql_policy=lambda: SimpleNamespace(timeout_ms=800),
        cypher_policy=lambda: SimpleNamespace(max_rows=7, timeout_ms=700),
        text_to_cypher=SimpleNamespace(enabled=False),
    )
    runtime = _Runtime(context=_Ctx(), policy=policy)  # type: ignore[arg-type]
    payload = await _list_schema(runtime)
    assert payload["error"] is None and payload["error_code"] is None

    # Codex #135 r1: the policy needs NO store, so it rides the DEGRADED
    # branches too — no-active-build/timeout/store-error are exactly the
    # states where an agent inspects its session before retrying
    from core.stores.repo import NoActiveBuildError

    class _NoBuildCtx:
        project = "museum"

        @asynccontextmanager
        async def bound(self):  # type: ignore[no-untyped-def]
            raise NoActiveBuildError("museum")
            yield deps

    degraded = await _list_schema(_Runtime(context=_NoBuildCtx(), policy=policy))  # type: ignore[arg-type]
    assert degraded["error_code"] == "NO_ACTIVE_BUILD"
    assert degraded["policy"] == payload["policy"]  # same disclosure, store or no store

    assert payload["policy"] == {
        # NO default_mode key: nothing on the MCP surface dispatches by it,
        # and disclosing it contradicts the instructions' hybrid_query
        # default (Codex #135 r4 — same rule as the cypher toggle)
        "max_top_k": 3,
        "max_graph_hops": 2,
        "max_sql_rows": 40,  # sql_rows(): the enforced, reconciled value
        "max_graph_rows": 7,  # cypher_policy().max_rows — likewise reconciled
        "sql_timeout_ms": 800,
        "graph_timeout_ms": 700,
        "max_latency_ms": 9000,
        "expose_debug": True,
        "sql_enabled": False,
        # NO nl_to_cypher/cypher_enabled key: the toggle's path is not
        # exposed on any MCP surface — disclosing it can only mislead
        "max_response_bytes": None,  # explicitly unbounded, not omitted
        "query_chars_cap": 4000,
        "browse_limit_cap": 200,
        "browse_q_cap": 64,
    }


def test_the_introspection_no_active_build_shape_is_explicit() -> None:
    """MCP12: the introspection tools' DR-001 refusal is the same explicit
    error-field shape as their timeout/store degradations — previously the
    NoActiveBuildError LookupError escaped them as a raw isError string."""
    from types import SimpleNamespace

    from core.mcp.server import _introspection_no_active_build, _Runtime

    runtime = _Runtime(
        context=SimpleNamespace(project="p"),  # type: ignore[arg-type]
        policy=SimpleNamespace(max_latency_ms=1000),  # type: ignore[arg-type]
    )
    payload = _introspection_no_active_build(runtime, "list_chunks")
    assert payload["build_id"] == "00000000-0000-0000-0000-000000000000"
    assert payload["subject"] == "list_chunks"
    assert "activate" in payload["error"]  # actionable (DR-001), not a repr


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
        "error_code": "QUERY_TIMEOUT",  # MCP13 (c): typed, not a free-text-only squash
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
            # MCP15: the disclosure block rides every branch, so the fake
            # carries the disclosed fields too
            default_mode="hybrid",
            max_top_k=20,
            max_graph_hops=3,
            max_sql_rows=50,
            expose_debug=False,
            text_to_cypher=SimpleNamespace(enabled=False),
            sql_rows=lambda: 50,
            cypher_policy=lambda: SimpleNamespace(max_rows=50, timeout_ms=500),
        )
        return _Runtime(context=_Ctx(), policy=policy)  # type: ignore[arg-type]

    timed_out = await _list_schema(_runtime(DBAPIError("q", None, _PgTimeout())))
    assert "deadline" in timed_out["error"]  # 57014 IS the §21 deadline
    assert timed_out["policy"]["max_top_k"] == 20  # MCP15: disclosure rides this branch too

    failed = await _list_schema(_runtime(DBAPIError("q", None, _PgOther())))
    # MCP2: the store is NAMED — "store unavailable" left the agent unable to
    # tell "route around Qdrant" from "everything is dead" (postgres down)
    assert failed["error"] == "postgres unavailable (DBAPIError) — §22"
    assert failed["build_id"] == "b-1"
    assert failed["policy"]["max_top_k"] == 20  # MCP15: ...and this one

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
    assert "bare name matches is still NOT evidence" in semantic
    # MCP7 REVERSED the MCP4/MCP5-era "no tool currently retrieves" pin (its
    # comment named exactly this trigger): entity mention refs now carry
    # chunk UUIDs (v1.1), so the description must point at the REAL path —
    # get_chunk — and the old impossibility claim must be gone. get_entity
    # remains a dead end for content and must stay unmentioned.
    assert "no tool currently retrieves" not in semantic
    assert "get_chunk" in semantic
    assert "get_entity" not in semantic


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
    """The raw chunk:{content_hash}:{ordinal} string is the STORED mention
    form — since MCP7 (v1.1) emitted entity refs carry chunk UUIDs, so an
    agent holding the raw string got it from somewhere stale; the error must
    NAME that (the #124 lesson: no bare "invalid", no dead ends) and
    validation must not cost a store round-trip."""
    repo = _IntrospectionRepo()
    payload = await _get_chunk(repo, "demo", "chunk:3626c139ab:0")
    assert payload["chunk"] is None
    assert "STORED form" in payload["error"]
    assert repo.queries == 0  # rejected before any store read

    document = await _get_document(repo, "demo", "not-a-uuid", _NO_EXPOSURE)
    assert document["document"] is None and "document UUID" in document["error"]
    assert repo.queries == 0


async def test_a_malformed_id_is_rejected_before_the_store_binding_opens() -> None:
    """Codex #125 r3: the tool wrappers rejected malformed ids only AFTER
    ``context.bound()`` — a bad id cost a Postgres active-build resolution,
    and with a store down the caller saw STORE_UNAVAILABLE instead of the
    actionable UUID error. The pre-binding check shares the helper's message
    (one constant, two emitters) and stamps the NIL build sentinel — no
    build was ever resolved, same convention as the pre-binding timeout."""
    from core.mcp.server import (
        _CHUNK_ID_MESSAGE,
        _DOCUMENT_ID_MESSAGE,
        _NIL_BUILD,
        _invalid_chunk_payload,
        _invalid_document_payload,
    )

    rejected = _invalid_chunk_payload("demo", "chunk:3626c139ab:0")
    assert rejected is not None
    assert rejected["build_id"] == _NIL_BUILD  # binding never happened
    assert rejected["chunk"] is None and rejected["error"] == _CHUNK_ID_MESSAGE

    doc_rejected = _invalid_document_payload("demo", "not-a-uuid")
    assert doc_rejected is not None
    assert doc_rejected["build_id"] == _NIL_BUILD
    assert doc_rejected["document"] is None and doc_rejected["error"] == _DOCUMENT_ID_MESSAGE

    # a VALID id passes through to the bound path — the check must not
    # over-block (the §22 dual)
    assert _invalid_chunk_payload("demo", str(uuid.uuid4())) is None
    assert _invalid_document_payload("demo", str(uuid.uuid4())) is None


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


class _BrowseRepo:
    """Fake repo for the MCP9 browse helpers: canned id-ordered rows; honors
    after_id/limit like the real keyset page, and records the q it saw."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.build_id = uuid.uuid4()
        self.rows = sorted(rows, key=lambda r: str(r.id))
        self.seen_q: list[str | None] = []

    async def page_entities(
        self,
        limit: int,
        after_id: Any = None,
        q: Any = None,
        entity_type: Any = None,
        fuzzy: bool = False,
    ) -> list[SimpleNamespace]:
        self.seen_q.append(q)
        rows = self.rows
        if q and fuzzy:
            rows = [r for r in rows if all(ch in r.canonical_name for ch in q)]
        elif q:
            rows = [r for r in rows if q.lower() in r.canonical_name.lower()]
        if entity_type:
            rows = [r for r in rows if r.type == entity_type]
        if after_id is not None:
            rows = [r for r in rows if str(r.id) > str(after_id)]
        return rows[:limit]

    async def page_rows(
        self, table: Any, columns: Any, limit: int, after_id: Any = None
    ) -> list[SimpleNamespace]:
        rows = self.rows
        if after_id is not None:
            rows = [r for r in rows if str(r.id) > str(after_id)]
        return rows[:limit]


def _entity_row(name: str, etype: str = "FACILITY") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), canonical_name=name, type=etype)


async def test_list_entities_pages_exhaustively_and_searches_substring() -> None:
    """MCP9's reason to exist: the max_top_k=20 retrieval ceiling made most
    of the corpus PERMANENTLY invisible ("list every option" was
    unanswerable in principle), and get_entity's exact-match zeroed
    near-miss names (主館 vs 主題館). Browsing walks the cursor to
    exhaustion, and q is substring — the near-miss now resolves."""
    from core.mcp.server import _list_entities

    rows = [_entity_row(f"廳-{i:02d}") for i in range(5)] + [_entity_row("主題館")]
    repo = _BrowseRepo(rows)

    # exhaustive walk at page size 2 → every entity reachable
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = await _list_entities(repo, "demo", 2, cursor, None, None)
        assert page["error"] is None
        seen.extend(e["name"] for e in page["entities"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(r.canonical_name for r in rows)  # ALL of them

    # substring first; when it finds NOTHING the character-AND fallback
    # kicks in and the response NAMES the looser mode — the visitor's 主館
    # (not a substring of 主題館) now resolves instead of zeroing
    found = await _list_entities(repo, "demo", 50, None, "題館", None)
    assert [e["name"] for e in found["entities"]] == ["主題館"]
    assert found["match"] == "substring"
    fuzzy = await _list_entities(repo, "demo", 50, None, "主館", None)
    assert [e["name"] for e in fuzzy["entities"]] == ["主題館"]
    assert fuzzy["match"] == "characters"  # precision drop is SAID


async def test_browse_cursors_refuse_foreign_scopes() -> None:
    """Class 31: a cursor pins its FULL result-set identity — replayed after
    an activation (different build) or with different filters it must be
    REFUSED with the cause named, never silently re-anchored onto a mixed
    result set; a garbage cursor is a typed error, not a crash."""
    from core.mcp.server import _list_entities

    repo = _BrowseRepo([_entity_row(f"e{i}") for i in range(4)])
    first = await _list_entities(repo, "demo", 2, None, None, None)
    cursor = first["next_cursor"]
    assert cursor is not None

    # same scope → continues
    ok = await _list_entities(repo, "demo", 2, cursor, None, None)
    assert ok["error"] is None and ok["entities"]

    # different q filter → refused, cause named
    refiltered = await _list_entities(repo, "demo", 2, cursor, "e", None)
    assert refiltered["entities"] == [] and "different listing scope" in refiltered["error"]

    # different entity_type filter → refused (the type axis is part of the
    # fingerprint — dropping it would silently page type-B's set from
    # type-A's anchor, the exact class-31 skip/dupe failure)
    retyped = await _list_entities(repo, "demo", 2, cursor, None, "EVENT")
    assert retyped["entities"] == [] and "different listing scope" in retyped["error"]

    # a cursor from ANOTHER TOOL → refused (tool name is in the fingerprint)
    from core.mcp.server import _list_chunks

    cross_tool = await _list_chunks(repo, "demo", 2, cursor)
    assert cross_tool["chunks"] == [] and "different listing scope" in cross_tool["error"]

    # different build (activation happened) → refused, cause named
    repo.build_id = uuid.uuid4()
    rebuilt = await _list_entities(repo, "demo", 2, cursor, None, None)
    assert rebuilt["entities"] == [] and "different build" in rebuilt["error"]

    # garbage → typed error
    garbage = await _list_entities(repo, "demo", 2, "not-a-cursor", None, None)
    assert "not a graphRAG browse cursor" in garbage["error"]


async def test_browse_limit_is_bounded_and_chunk_previews_name_truncation() -> None:
    """The browse limit is validated (typed error, no store read shape) and
    list_chunks previews carry a NAMED truncation flag — a silent cut would
    read as the full text (§22); get_chunk is the full-text path."""
    from core.mcp.server import _list_chunks, _list_entities

    repo = _BrowseRepo([])
    over = await _list_entities(repo, "demo", 999, None, None, None)
    assert "limit must be an integer in 1..200" in over["error"]
    bad = await _list_entities(repo, "demo", True, None, None, None)
    assert bad["error"] is not None  # bool is not a page size

    # Codex #129 r2: q is agent-controlled and fuzzy costs one predicate per
    # character — an unbounded q must be refused typed, and the fallback only
    # engages for short name-ish probes (a 17+-char zero-hit stays substring)
    long_q = await _list_entities(repo, "demo", 50, None, "長" * 65, None)
    assert "at most 64 characters" in long_q["error"]
    no_fuzzy = await _list_entities(repo, "demo", 50, None, "無" * 17, None)
    assert no_fuzzy["error"] is None and no_fuzzy["match"] == "substring"
    assert no_fuzzy["entities"] == []  # honest empty, not a predicate storm

    long_chunk = SimpleNamespace(
        id=uuid.uuid4(), document_id=uuid.uuid4(), ordinal=0, text="長" * 300
    )
    short_chunk = SimpleNamespace(id=uuid.uuid4(), document_id=uuid.uuid4(), ordinal=1, text="短")
    chunks = await _list_chunks(_BrowseRepo([long_chunk, short_chunk]), "demo", 50, None)
    by_ordinal = {c["ordinal"]: c for c in chunks["chunks"]}
    assert len(by_ordinal[0]["text_preview"]) == 200 and by_ordinal[0]["text_truncated"]
    assert by_ordinal[1]["text_preview"] == "短" and not by_ordinal[1]["text_truncated"]


def test_a_query_the_store_cannot_hold_is_an_input_refusal_not_a_store_outage() -> None:
    """QA5 D5/D9/D11: the §16 query gate used to pass three malformed classes
    straight through to the stores, and each came back wearing the WRONG
    diagnosis.

    A NUL (or unpaired surrogate) reached Postgres, raised a DBAPIError, and
    the §22 handler faithfully relabelled it STORE_UNAVAILABLE "postgres
    unavailable" — one byte from the caller and the server LIED about
    infrastructure health, telling a contract-abiding agent to back off from a
    database that was fine. A whitespace-only query ran a real retrieval and
    scored the corpus's own placeholder entity first: a confident answer to a
    question nobody asked. Both must be refused BEFORE binding, in the
    vocabulary that names the caller's own input."""
    from core.mcp.server import _unusable_query_payload

    for bad, why in (
        ("海科館" + chr(0), "NUL"),
        ("\ud800", "unpaired surrogate"),
        ("   \t\n  ", "whitespace only"),
        ("", "empty"),
    ):
        payload = _unusable_query_payload("demo", "semantic_search", bad)
        assert payload is not None, why
        # nil build: nothing was ever bound, so no store was consulted
        assert payload["build_id"] == "00000000-0000-0000-0000-000000000000", why
        assert payload["results"] == [], why
        codes = [w["code"] for w in payload["warnings"]]
        assert codes == ["GUARDRAIL_BLOCKED"], why  # never STORE_UNAVAILABLE
        # the echo must survive the response's OWN utf-8 encoding: echoing a
        # lone surrogate whole turned the refusal itself into a raw
        # UnicodeEncodeError, so the caller learned nothing about its mistake
        payload["query"].encode("utf-8")

    # the over-block dual: a normal question is NOT refused, and the §21
    # length cap still is (the guard absorbed that check, it did not lose it)
    assert _unusable_query_payload("demo", "semantic_search", "票價多少") is None
    assert _unusable_query_payload("demo", "semantic_search", "x" * 4000) is None
    over = _unusable_query_payload("demo", "semantic_search", "x" * 4001)
    assert over is not None and "4001" in over["warnings"][0]["message"]


def test_an_unusable_introspection_subject_is_typed_before_binding() -> None:
    """QA5 D5: get_entity had NO pre-binding validation, so a NUL in ``name``
    became the same "postgres unavailable" lie. A blank exact-name lookup can
    only be a caller bug — but a blank ``q`` is the documented
    browse-everything case, so the guard must tell those two apart rather than
    refusing both."""
    from core.mcp.server import _unusable_subject_payload

    bad = _unusable_subject_payload("demo", "name", "海科館" + chr(0))
    assert bad is not None
    assert bad["error_code"] == "INVALID_INPUT"  # not STORE_UNAVAILABLE
    assert bad["build_id"] == "00000000-0000-0000-0000-000000000000"
    assert "not a store outage" in bad["error"]
    bad["subject"].encode("utf-8")  # sanitized echo, encodable

    assert _unusable_subject_payload("demo", "name", "  ") is not None  # blank name = bug
    assert _unusable_subject_payload("demo", "q", "", allow_blank=True) is None  # blank q = browse
    assert _unusable_subject_payload("demo", "name", "票價資訊") is None  # a good name passes


def test_safe_echo_bounds_its_work_to_the_window_not_the_whole_input() -> None:
    """QA5 (Codex #140 r4): the echo is advertised as windowed to `limit`, so a
    multi-MB malformed argument must not drive an O(len) transform before the
    typed refusal comes back — the caller is refused either way, so only the
    echoed prefix is worth touching. Slicing BEFORE the substitution is what
    makes the window bound the work, not just the output.

    Pinned so it can't regress to transform-then-slice: a value whose slice is
    cheap but whose full iteration EXPLODES passes only if the slice happens
    first. (Slicing is 1:1 with the substitution, so the result is unchanged —
    the plain cases below assert the truncation and U+FFFD mapping still hold.)"""
    from core.mcp.server import _safe_echo

    assert _safe_echo("x" * 500) == "x" * 200  # default window
    assert _safe_echo("ab" + chr(0) + "c", 80) == "ab�c"  # substitution intact
    assert _safe_echo("\ud800" * 10, 3) == "�" * 3  # window applies to surrogates

    class _CheapSliceExplodingIter(str):
        """__getitem__(slice) is a normal small str; iterating the WHOLE thing
        raises — so transform-then-slice (which iterates `value`) blows up,
        while slice-then-transform (which iterates `value[:limit]`) does not."""

        def __iter__(self) -> Any:
            raise AssertionError("_safe_echo iterated the whole value instead of the window")

        def __getitem__(self, key: Any) -> Any:
            return "safe-prefix" if isinstance(key, slice) else super().__getitem__(key)

    assert _safe_echo(cast(str, _CheapSliceExplodingIter("huge")), 80) == "safe-prefix"


def test_every_refusal_echo_survives_its_own_serialization() -> None:
    """QA5 D11, swept to the refusals that ALREADY rejected the input.

    ``get_chunk``/``get_document`` parse the id and refuse a malformed one, so
    the surrogate never reaches a store — but the refusal echoed the id RAW,
    and the response then died encoding ITSELF: the caller got a transport
    UnicodeEncodeError instead of the perfectly good "not a UUID" answer the
    server had already computed. A guard that rejects the input but cannot
    SAY so is not a guard; every echo of caller text needs the same
    treatment, not just the one the defect was reported against."""
    import json

    from core.mcp.server import (
        _invalid_chunk_payload,
        _invalid_document_payload,
        _unusable_param_payload,
        _unusable_query_payload,
        _unusable_subject_payload,
    )

    bad = "\ud800"
    for label, payload in (
        ("get_chunk", _invalid_chunk_payload("demo", bad)),
        ("get_document", _invalid_document_payload("demo", bad)),
        ("get_entity", _unusable_subject_payload("demo", "name", bad)),
        ("graph seed", _unusable_param_payload("demo", "graph_query", "lbl", "entity", bad)),
        ("query", _unusable_query_payload("demo", "semantic_search", bad)),
    ):
        assert payload is not None, label
        # the response is serialized to JSON and encoded as utf-8 by the
        # transport — a lone surrogate anywhere in it kills the whole answer
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # and the substitution character is U+FFFD, not the "?" that
        # encode(..., "replace") produces: the echo exists to show the SHAPE
        # of the input, and a "?" is indistinguishable from one the caller
        # actually typed
        assert "�" in json.dumps(payload, ensure_ascii=False), label


def test_the_seed_and_filter_parameters_are_guarded_like_the_query() -> None:
    """QA5 D5 sibling sweep: ``_bounded`` guards the echoed LABEL, not the
    traversal parameters, and ``label = query or f"{template}({entity})"`` —
    so a caller supplying a clean ``query`` slipped a malformed SEED straight
    through to the Postgres name lookup and drew the same fake
    "postgres unavailable". ``entity_type`` is the matching hole on the browse
    side: it rides the very same ``page_entities`` call as the ``q`` filter."""
    from core.mcp.server import _unusable_param_payload, _unusable_subject_payload

    nul = "海科館" + chr(0)
    for field in ("entity", "other_entity"):
        refusal = _unusable_param_payload("demo", "graph_query", "a clean label", field, nul)
        assert refusal is not None, field
        assert refusal["build_id"] == "00000000-0000-0000-0000-000000000000", field
        assert [w["code"] for w in refusal["warnings"]] == ["GUARDRAIL_BLOCKED"], field
        assert field in refusal["warnings"][0]["message"], field  # NAMES the parameter

    assert _unusable_subject_payload("demo", "entity_type", nul, allow_blank=True) is not None
    # the over-block dual: real seeds and a real type filter still pass
    assert _unusable_param_payload("demo", "graph_query", "lbl", "entity", "票價資訊") is None
    assert _unusable_subject_payload("demo", "entity_type", "EVENT", allow_blank=True) is None

    # NO refusal message may describe the ECHO's contents. Four revisions
    # tried to and were wrong four different ways — the substitution character
    # was really "?"; the value was not echoed at all; a blank value carried
    # the claim; a long blank re-earned it through truncation; a bad unit past
    # the 80-char window fell outside it. The claim and the artifact were
    # produced by different code under different rules, so patching the
    # predicate again would only move the flaw. The reason names the offending
    # CODE POINT instead, which is what the caller must act on, and a message
    # that asserts nothing about the echo can never be wrong about it.
    cases = (
        (" " * 3, "short blank"),
        (" " * 300, "blank past the echo window"),
        ("\ud800", "mangled at index 0"),
        ("x" * 100 + chr(0), "mangled PAST the echo window"),  # the uncovered shape
    )
    for value, why in cases:
        refusal = _unusable_param_payload("demo", "semantic_search", "lbl", "query", value)
        assert refusal is not None, why
        message = refusal["warnings"][0]["message"]
        assert "U+FFFD" not in message, why  # never claims anything about the echo
        assert message.encode("utf-8"), why  # and still serializes
    # the reason stays actionable WITHOUT the echo: it names the code point
    named = _unusable_param_payload("demo", "t", "lbl", "query", "x" * 100 + chr(0))
    assert named is not None and "U+0000" in named["warnings"][0]["message"]


def test_framework_argument_failures_answer_typed_without_leaking_internals() -> None:
    """QA5 D10/D12: a TYPE-level argument error never reached a tool body, so
    it escaped the documented error vocabulary entirely — the caller got
    pydantic's raw dump naming the SDK's generated model
    (``semantic_searchArguments``) and the pinned pydantic version's docs URL:
    internals an unauthenticated caller cannot act on and should not see.

    D12 is the same seam from the other side: pydantic coerces JSON ``true``
    into 1 BEFORE any body runs, so the server's own ``type(limit) is not int``
    guard was dead code and ``{"limit": true}`` became a successful 1-item page
    — a client bug given a green light. ``false`` was worse: it arrived as 0
    and the range error quoted "0", a value the caller never sent."""
    from mcp import types as _mcp_types
    from pydantic import BaseModel, ValidationError

    from core.mcp.server import _argument_refusal, _readable_validation_error

    class _Args(BaseModel):
        query: str

    try:
        _Args(query=cast(str, None))
        raise AssertionError("expected a validation error")
    except ValidationError as exc:
        rendered = _readable_validation_error(exc)
    assert rendered.startswith("query: ")  # the parameter is NAMED
    assert "_Args" not in rendered and "pydantic.dev" not in rendered  # no internals

    # an INTROSPECTION tool's refusal keeps that family's error shape, which
    # is what those tools advertise
    refusal = _argument_refusal(
        "list_entities",
        "limit: expected an integer, got the boolean true",
        project="demo",
        arguments={"limit": True},
    )
    assert refusal.isError is True
    block = refusal.content[0]
    assert isinstance(block, _mcp_types.TextContent)
    assert refusal.structuredContent == {"error": block.text, "error_code": "INVALID_INPUT"}
    # a CallToolResult is handed back VERBATIM by the SDK rather than being
    # re-reported as an "Output validation error" that would bury the real
    # mistake — but bypassing OUR validator is not the same as being valid.
    # The envelope tools advertise the FROZEN §16 contract as their
    # outputSchema, so a schema-driven client deserializes structuredContent
    # against it and a bare {error, error_code} fails on ITS side: the refusal
    # would be unreadable to exactly the careful clients that read the schema.
    assert isinstance(refusal, _mcp_types.CallToolResult)

    import json

    import jsonschema

    from core.mcp.server import _ENVELOPE_TOOLS

    validator = jsonschema.Draft202012Validator(
        json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8")),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    for tool in _ENVELOPE_TOOLS:
        for arguments, why in (
            ({"query": None}, "the query itself is the invalid argument"),
            ({"query": "票價多少", "top_k": 2.5}, "a sibling argument is invalid"),
        ):
            envelope = _argument_refusal(
                tool, "query: Input should be a valid string", project="demo", arguments=arguments
            )
            structured = envelope.structuredContent
            assert structured is not None, (tool, why)
            validator.validate(structured)  # the CLIENT's own check, not ours
            # explain_retrieval RUNS the hybrid router and the contract's tool
            # enum lists only the five modes — its envelopes say hybrid_query,
            # exactly as its success path already does
            assert structured["tool"] == _ENVELOPE_TOOLS[tool], (tool, why)
            assert structured["results"] == [], (tool, why)
            assert structured["build_id"] == "00000000-0000-0000-0000-000000000000", (tool, why)
            assert [w["code"] for w in structured["warnings"]] == ["GUARDRAIL_BLOCKED"], (tool, why)
            # a non-string query cannot be echoed as one; a usable one is kept
            expected = arguments["query"] if isinstance(arguments["query"], str) else ""
            assert structured["query"] == expected, (tool, why)

    # The mapping CLAIMS what each tool reports in `tool`, but the tool bodies
    # are what actually report it — a third owner the mapping does not reach.
    # Rather than trust the claim, read the artifact: the set of names the
    # bodies hand to _bounded must be exactly the set the mapping promises, so
    # editing a literal without the mapping (or the reverse) makes the refusal
    # and the success path disagree about which tool answered.
    import inspect
    import re

    import core.mcp.server as _server_module

    reported = set(re.findall(r'_bounded\(rt, "([^"]+)"', inspect.getsource(_server_module)))
    assert reported == set(_ENVELOPE_TOOLS.values()), (reported, set(_ENVELOPE_TOOLS.values()))


async def test_every_pre_binding_claim_is_pinned_by_a_raising_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These refusals used to also TELL the caller "rejected before any store
    was read" — but that clause is now GONE from the message (Codex #140 r6),
    because the caller set was not closed after all. Round 3 concluded the MCP
    claim was keepable because its callers were "these tool bodies"; that
    enumeration missed the seventh caller — the REST facade's
    ``run_bounded_query`` reaches the SAME ``_unusable_query_payload`` AFTER
    ``_load_policy`` has read the store, so the claim was false on that path
    exactly as its REST twin was. The rule (assert only what you can guarantee)
    was right; the caller enumeration under it was incomplete. What survives in
    the message is the ANTI-BACK-OFF half — "this is an INPUT problem, not a
    store outage" — which is true from every call site.

    The ORDERING is now a property THIS TEST guarantees rather than a promise
    printed in shared text (the correct form, round 3): binding RAISES, and
    every MCP guard must still answer, which is only possible if it runs first.
    Its machine-readable form is ``build_id == _NIL_BUILD`` — no build was ever
    resolved, because nothing tried to."""
    import json

    from mcp import types as _mcp_types

    import core.mcp.server as server_module
    from core.mcp.server import build_server

    monkeypatch.setattr(server_module, "chat_model", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "query_embedding_model", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "vector_client", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "graph_driver", lambda: cast(Any, object()))
    monkeypatch.setattr(
        server_module, "create_async_engine", lambda *a, **k: SimpleNamespace(dispose=None)
    )
    server = build_server("demo")

    def _explode() -> Any:
        raise AssertionError("the guard must refuse BEFORE the build is bound")

    runtime = SimpleNamespace(
        context=SimpleNamespace(project="demo", bound=_explode),
        policy=SimpleNamespace(max_latency_ms=10_000, max_top_k=20),
        exposure=None,
    )
    monkeypatch.setattr(
        server,
        "get_context",
        lambda: SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime)),
    )
    handler = server._mcp_server.request_handlers[_mcp_types.CallToolRequest]  # noqa: SLF001
    nul = "海科館" + chr(0)

    for tool, arguments in (
        ("get_entity", {"name": nul}),  # _unusable_subject_payload
        ("list_entities", {"q": nul}),  # the same, via the browse filter
        ("semantic_search", {"query": nul}),  # _unusable_query_payload, inside _bounded
        ("graph_query", {"template": "neighbors", "entity": nul}),  # _unusable_param_payload
        ("hybrid_query", {"query": "票價", "graph_template": "neighbors", "graph_entity": nul}),
    ):
        request = _mcp_types.CallToolRequest(
            method="tools/call",
            params=_mcp_types.CallToolRequestParams(name=tool, arguments=arguments),
        )
        result = cast(_mcp_types.CallToolResult, (await handler(request)).root)
        structured = result.structuredContent
        assert structured is not None, tool
        # the nil sentinel is the machine-readable half of the same claim: no
        # build was ever resolved, because nothing ever tried to resolve one
        assert structured["build_id"] == "00000000-0000-0000-0000-000000000000", tool
        # ...and the ordering is guaranteed by THIS test, not asserted in the
        # message — the shared helper carries no "before any store" clause (it
        # would be false on the REST path, whose run_bounded_query reaches the
        # same helper after _load_policy has read the store, Codex #140 r6),
        # only the anti-back-off half that holds from every call site
        blob = json.dumps(structured, ensure_ascii=False)
        assert "before any store" not in blob, tool
        assert "not a store outage" in blob, tool


async def test_a_miss_is_not_found_and_a_junk_cursor_does_not_blame_the_build() -> None:
    """Two ways the typed vocabulary lied to a consumer that read the docs.

    (a) The initialize instructions define ONE taxonomy for the get_*/list_*
    family — "NOT_FOUND (the id is not in the active build)" and "error_code
    null = success". `get_chunk`/`get_document` honour it; `get_entity` did
    not, answering a miss with `error_code: null` and `entities: []`. A
    consumer branching on error_code exactly as instructed read a renamed
    entity or a stale citation as a SUCCESSFUL empty answer (QA6/D8).

    (b) `_parse_browse_cursor` split on "|" and compared the first segment to
    the build id without checking it was a UUID, so ANY three-segment string
    reported "the active build changed" (QA6/D13). In a DR-001 system that is
    an operationally weighty claim — it sends an operator to audit builds when
    nothing happened. Shape must be established before a diagnosis names a
    cause. The real build-mismatch case must survive, which is the second half
    of this test: fixing a false alarm by deleting the true alarm is no fix.
    """
    import uuid as _uuid

    from core.mcp.server import _get_entity, _parse_browse_cursor

    class _Repo:
        build_id = _uuid.uuid4()

        def __init__(self, ids: list[Any]) -> None:
            self._ids = ids

        async def entity_ids_by_name(self, name: str) -> list[Any]:
            return self._ids

        async def mentions_by_entity(self, ids: Any) -> dict[Any, Any]:
            return {}

        async def chunks_by_content_ref(self, pairs: Any) -> dict[Any, Any]:
            return {}

    miss = await _get_entity(cast(Any, _Repo([])), "demo", "Nobody")
    assert miss["error_code"] == "NOT_FOUND"  # was null — a miss read as success
    assert miss["entities"] == []  # the shape callers already rely on is kept
    blank = await _get_entity(cast(Any, _Repo([])), "demo", "  ")
    assert blank["error_code"] == "INVALID_INPUT"  # a bad ARGUMENT stays distinct

    build = "11111111-1111-1111-1111-111111111111"
    other = "00000000-0000-0000-0000-000000000001"
    last = "22222222-2222-2222-2222-222222222222"

    _, junk = _parse_browse_cursor("a|b|c", build, "scope")
    assert junk is not None and "not a graphRAG browse cursor" in junk
    assert "active build changed" not in junk, "junk must not raise a build alarm"

    # ...and "is a UUID" is NOT enough (gate-2 on #144): uuid.UUID() also
    # accepts de-dashed hex, urn:uuid: and braced forms, so a cursor naming
    # THIS VERY BUILD in a non-canonical spelling still failed the string
    # compare and drew the same false alarm. We only ever mint str(build_id),
    # so anything else is a cursor we did not mint — "not ours", not "your
    # build moved".
    for spelling in (build.replace("-", ""), f"urn:uuid:{build}", "{" + build + "}"):
        _, odd = _parse_browse_cursor(f"{spelling}|scope|{last}", build, "scope")
        assert odd is not None and "not a graphRAG browse cursor" in odd, spelling
        assert "active build changed" not in odd, spelling

    # ...and the TRUE alarm still fires for a well-formed foreign build id
    _, moved = _parse_browse_cursor(f"{other}|scope|{last}", build, "scope")
    assert moved is not None and "active build changed" in moved
    # a matching build with a different scope keeps its own distinct cause
    _, rescoped = _parse_browse_cursor(f"{build}|other|{last}", build, "scope")
    assert rescoped is not None and "different listing scope" in rescoped
    # and a well-formed cursor still works
    after, ok = _parse_browse_cursor(f"{build}|scope|{last}", build, "scope")
    assert ok is None and after == _uuid.UUID(last)


async def test_an_unknown_parameter_is_refused_and_the_rule_is_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misspelled parameter must not return a CLEAN SUCCESS (QA3/D3).

    The generated arg model carries pydantic's default ``extra="ignore"``, so
    ``semantic_search {"query": …, "topk": 3}`` DROPPED the typo, let ``top_k``
    fall back to its default, and answered with 20 results, ``warnings: []``
    and no error code — an answer to a different question than the one asked,
    presented as success. The callers here are LLM agents, i.e. exactly the
    population that emits plausible-but-wrong parameter names, and REST already
    refuses the identical typo with ``400 extra_forbidden``.

    Both halves are pinned, because enforcing without advertising leaves the
    published schema still saying extra keys are fine (the QA2 lesson: taking a
    behavior without the sentence it makes true):
      * the dispatch guard REFUSES, naming the offending key and the allowlist;
      * every advertised ``inputSchema`` says ``additionalProperties: false``.
    """
    import json

    from mcp import types as _mcp_types

    import core.mcp.server as server_module
    from core.mcp.server import build_server

    for factory in ("chat_model", "query_embedding_model", "vector_client", "graph_driver"):
        monkeypatch.setattr(server_module, factory, lambda: cast(Any, object()))
    monkeypatch.setattr(
        server_module, "create_async_engine", lambda *a, **k: SimpleNamespace(dispose=None)
    )
    server = build_server("demo")

    def _explode() -> Any:
        raise AssertionError("an unknown parameter must be refused BEFORE binding")

    runtime = SimpleNamespace(
        context=SimpleNamespace(project="demo", bound=_explode),
        policy=SimpleNamespace(max_latency_ms=10_000, max_top_k=20),
        exposure=None,
    )
    monkeypatch.setattr(
        server,
        "get_context",
        lambda: SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime)),
    )
    handler = server._mcp_server.request_handlers[_mcp_types.CallToolRequest]  # noqa: SLF001

    async def call(tool: str, arguments: dict[str, Any]) -> str:
        request = _mcp_types.CallToolRequest(
            method="tools/call",
            params=_mcp_types.CallToolRequestParams(name=tool, arguments=arguments),
        )
        result = cast(_mcp_types.CallToolResult, (await handler(request)).root)
        return json.dumps(result.structuredContent, ensure_ascii=False)

    # the three reported repros — each returned a clean success before
    typo = await call("semantic_search", {"query": "票價多少", "topk": 3})
    assert "topk: unknown parameter" in typo and "top_k" in typo  # names the fix
    wrong_name = await call("list_entities", {"entity": "票價資訊", "limit": 3})
    assert "entity: unknown parameter" in wrong_name and "INVALID_INPUT" in wrong_name
    singular = await call("graph_query", {"template": "neighbors", "entity": "x", "hop": 2})
    assert "hop: unknown parameter" in singular

    # a tool that declares NO arguments makes every key unknown — correct, and
    # it must not crash on the empty allowlist
    assert "this tool accepts no arguments" in await call("list_schema", {"anything": 1})
    # ...and a correct call is untouched: refusing the typo must not over-block
    assert "unknown parameter" not in await call("semantic_search", {"query": "ok", "top_k": 3})

    # The KEY is caller-chosen bytes, so this refusal is an ECHO PATH and takes
    # the same two guards as every other one here. Without _safe_echo a
    # surrogate in a key kills the response in serialization and the caller
    # gets NOTHING back — QA5/D11 reopened through new machinery one commit
    # after it was closed (Codex/gate-2 on #143).
    surrogate = _mcp_types.CallToolRequest(
        method="tools/call",
        params=_mcp_types.CallToolRequestParams(
            name="semantic_search", arguments={"query": "hi", "\ud800bad": 1}
        ),
    )
    refused = cast(_mcp_types.CallToolResult, (await handler(surrogate)).root)
    # the transport serializes the ServerResult — that is where D11 died
    _mcp_types.ServerResult(refused).model_dump_json().encode("utf-8")

    # ...and the echo is WINDOWED: a huge key or a flood of keys must not be
    # reflected whole (#133 r1 — no refusal path reflects a large input)
    huge = await call("semantic_search", {"query": "hi", "K" * 5000: 1})
    assert len(huge) < 1000, "an oversized key must not be reflected whole"
    flood = await call("semantic_search", {"query": "hi", **{f"k{i}": 1 for i in range(200)}})
    assert len(flood) < 1000 and "more)" in flood  # says how many it withheld

    # the ADVERTISED half: a schema-reading client sees the rule rather than
    # discovering it by being refused
    listed = cast(
        _mcp_types.ListToolsResult,
        (
            await server._mcp_server.request_handlers[_mcp_types.ListToolsRequest](  # noqa: SLF001
                _mcp_types.ListToolsRequest(method="tools/list")
            )
        ).root,
    )
    assert listed.tools, "no tools advertised"
    assert all(t.inputSchema.get("additionalProperties") is False for t in listed.tools)


async def test_the_browse_filter_is_capped_on_length_before_the_storability_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_entities(q=...)`` refuses an oversized q on LENGTH before it runs
    the per-character storability scan (Codex #140 r7). The MCP input schema
    puts no ceiling on q, and the surrogate/NUL scan is O(n) in that length, so
    a value that BROWSE_Q_CAP will always refuse must be rejected in O(1) first
    — the same length-first ordering ``_unusable_query_payload`` already gives
    the retrieval query (Codex #140 r5).

    The probe is a q that is BOTH oversized AND unstorable (a trailing NUL): the
    two orderings print DIFFERENT messages, so the message names which ran
    first. Length-first says "at most 64 characters"; a regression that scanned
    first would say "NUL". Asserting the length wording (and the ABSENCE of the
    NUL wording) pins the order, not merely that some refusal came back — a
    plain oversized q would be refused either way and prove nothing. The raising
    ``bound`` keeps it honest that this all happens before any store read."""
    import json

    from mcp import types as _mcp_types

    import core.mcp.server as server_module
    from core.mcp.server import BROWSE_Q_CAP, build_server

    monkeypatch.setattr(server_module, "chat_model", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "query_embedding_model", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "vector_client", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "graph_driver", lambda: cast(Any, object()))
    monkeypatch.setattr(
        server_module, "create_async_engine", lambda *a, **k: SimpleNamespace(dispose=None)
    )
    server = build_server("demo")

    def _explode() -> Any:
        raise AssertionError("length must be capped BEFORE any store is bound")

    runtime = SimpleNamespace(
        context=SimpleNamespace(project="demo", bound=_explode),
        policy=SimpleNamespace(max_latency_ms=10_000, max_top_k=20),
        exposure=None,
    )
    monkeypatch.setattr(
        server,
        "get_context",
        lambda: SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime)),
    )
    handler = server._mcp_server.request_handlers[_mcp_types.CallToolRequest]  # noqa: SLF001

    # oversized (66 > BROWSE_Q_CAP) AND unstorable (trailing NUL): the message
    # tells us which guard ran first
    oversized_and_unstorable = "長" * (BROWSE_Q_CAP + 1) + chr(0)
    request = _mcp_types.CallToolRequest(
        method="tools/call",
        params=_mcp_types.CallToolRequestParams(
            name="list_entities", arguments={"q": oversized_and_unstorable}
        ),
    )
    result = cast(_mcp_types.CallToolResult, (await handler(request)).root)
    structured = result.structuredContent
    assert structured is not None
    blob = json.dumps(structured, ensure_ascii=False)
    assert f"at most {BROWSE_Q_CAP} characters" in blob  # refused on LENGTH...
    assert "NUL" not in blob and "surrogate" not in blob  # ...scan never ran
    assert structured["build_id"] == "00000000-0000-0000-0000-000000000000"


async def test_the_dispatch_guard_is_actually_wired_into_the_registered_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA5 D10/D12, driven through the REAL seam rather than by hand.

    Asserting on the refusal builders alone leaves the wiring unpinned:
    ``_guard_tool_dispatch(server)`` could be deleted outright and a
    builder-only test stays green, which is exactly the false-green this
    project treats as a defect. So this drives
    ``request_handlers[CallToolRequest]`` — the handler an actual MCP client
    reaches — and pins that BOTH classes come back typed.

    It also tripwires the SDK internals the guard reads: the bool check walks
    ``_tool_manager._tools[...].parameters``, and every accessor on that path
    is a ``getattr``/``.get`` that degrades SILENTLY to "no refusal" if the
    SDK renames a field — the leak would reopen with every test still green
    (the MCP17 ``_evict_from_manager`` lesson). The same reasoning covers
    ``ToolError.__cause__``: the guard's primary branch depends on the SDK
    wrapping validation errors, so a version that stops wrapping must turn
    this red rather than quietly fall through to the defensive branch."""
    import inspect

    import mcp.server.fastmcp.tools.base as _sdk_tool_base
    from mcp import types as _mcp_types
    from mcp.server.fastmcp.exceptions import ToolError
    from pydantic import ValidationError

    import core.mcp.server as server_module
    from core.mcp.server import build_server

    monkeypatch.setattr(server_module, "chat_model", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "query_embedding_model", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "vector_client", lambda: cast(Any, object()))
    monkeypatch.setattr(server_module, "graph_driver", lambda: cast(Any, object()))
    monkeypatch.setattr(
        server_module, "create_async_engine", lambda *a, **k: SimpleNamespace(dispose=None)
    )
    server = build_server("demo")

    # --- SDK attribute tripwires: the guard reads these; a rename must be red
    tool = server._tool_manager._tools["list_entities"]  # noqa: SLF001
    assert isinstance(tool.parameters, dict)  # the schema the bool check reads
    assert "limit" in tool.parameters.get("properties", {})
    assert issubclass(ToolError, Exception)
    src = inspect.getsource(_sdk_tool_base)
    assert "raise ToolError" in src and "from e" in src, (
        "the backend-fault case below fakes how the SDK surfaces a tool-body "
        "failure (ToolError chained from the original) — if the SDK stopped "
        "chaining, that fake would stop reproducing reality and the test would "
        "be pinning a scenario that no longer occurs"
    )
    assert issubclass(ValidationError, Exception)

    handler = server._mcp_server.request_handlers[_mcp_types.CallToolRequest]  # noqa: SLF001

    async def _call(name: str, arguments: dict[str, Any]) -> _mcp_types.CallToolResult:
        request = _mcp_types.CallToolRequest(
            method="tools/call",
            params=_mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        result = await handler(request)
        return cast(_mcp_types.CallToolResult, result.root)

    # D12: pydantic coerces JSON true -> 1 BEFORE any body runs, so the
    # server's own `type(limit) is not int` guard was dead code and this used
    # to be a perfectly successful 1-item page
    boolean = await _call("list_entities", {"limit": True})
    assert boolean.isError is True
    assert boolean.structuredContent == {
        "error": cast(_mcp_types.TextContent, boolean.content[0]).text,
        "error_code": "INVALID_INPUT",
    }
    assert "boolean true" in cast(_mcp_types.TextContent, boolean.content[0]).text

    # false was worse: it arrived as 0 and the range error quoted "0", a value
    # the caller never sent
    assert (
        "boolean false"
        in cast(
            _mcp_types.TextContent, (await _call("list_entities", {"limit": False})).content[0]
        ).text
    )

    # The STRINGIFIED form {"limit": "true"} does NOT become a successful
    # 1-item page (Codex #140 r5 worried it would): `pre_parse_json` leaves it
    # as the string "true", because `json.loads("true")` is the bool True and
    # a bool is an int subclass, so the SDK's `isinstance(parsed, str|int|float)`
    # guard skips it. Pydantic then rejects the string against `int` — verified
    # here rather than reasoned about — so it is a typed INVALID_INPUT refusal,
    # never a coerced 1.
    stringified = await _call("list_entities", {"limit": "true"})
    assert stringified.isError is True
    assert stringified.structuredContent is not None
    assert stringified.structuredContent["error_code"] == "INVALID_INPUT"
    # and NOT a success carrying results/a cursor (what "coerced to 1" would be)
    assert "results" not in stringified.structuredContent

    # D10: a type error never reached a tool body, so it escaped the error
    # vocabulary entirely — pydantic's raw dump named the SDK's generated
    # model and linked the pinned pydantic version's docs
    typed = await _call("semantic_search", {"query": None})
    assert typed.isError is True
    text = cast(_mcp_types.TextContent, typed.content[0]).text
    assert "query: " in text  # the parameter is NAMED
    assert "Arguments" not in text and "pydantic.dev" not in text  # no internals

    # Our verdict must equal the SDK's, and it only does if we validate the
    # SAME VALUE: FastMCP validates pre_parse_json(arguments), because clients
    # JSON-stringify argument values (its own note says Claude desktop "seems
    # incapable of NOT doing this"). Validating the raw mapping diverges in
    # BOTH directions, so both are pinned.
    # (i) a stringified value the SDK parses into something ILLEGAL: judging
    # the raw string as a valid str let it through to the SDK's generic error,
    # reopening D10's leak verbatim.
    parsed_bad = await _call("list_entities", {"q": "[1]"})
    assert parsed_bad.isError is True
    bad_text = cast(_mcp_types.TextContent, parsed_bad.content[0]).text
    assert "Arguments" not in bad_text and "pydantic.dev" not in bad_text
    assert parsed_bad.structuredContent is not None  # our typed refusal, not the SDK's
    # (ii) a stringified value the SDK parses into something LEGAL: refusing it
    # would block a call the server would otherwise answer (the over-block dual)
    parsed_ok = await _call("semantic_search", {"query": "票價多少", "top_k": "null"})
    # it must not be OUR refusal: pre-parsing yields a legal None, so the call
    # proceeds (and then fails on this fixture's absent request context, which
    # is the SDK's own generic error — content only, no structuredContent)
    assert parsed_ok.structuredContent is None

    # A ValidationError raised by the TOOL BODY (or by a dependency decoding
    # malformed store data) is a BACKEND fault, not the caller's mistake. The
    # SDK wraps every tool failure in ToolError with the original on
    # __cause__, so classifying by exception TYPE would relabel it
    # INVALID_INPUT and tell a client to fix a request that was fine — the
    # very misdiagnosis this task exists to end, reintroduced by its own fix.
    # Arguments are therefore validated BEFORE dispatch, so the phase decides:
    # anything raised past that point flows out as what it is.
    from pydantic import BaseModel as _BaseModel

    class _StoreRow(_BaseModel):
        count: int

    async def _exploding(name: str, arguments: dict[str, Any]) -> Any:
        # exactly how the SDK surfaces a fault from inside a tool body:
        # fastmcp/tools/base.py raises ToolError(...) FROM the original, so
        # __cause__ is a ValidationError that has nothing to do with the
        # caller's arguments (here: malformed data decoded from a store)
        try:
            _StoreRow(count=cast(int, "not a number"))
        except ValidationError as exc:
            raise ToolError(f"Error executing tool {name}: {exc}") from exc
        raise AssertionError("unreachable")

    monkeypatch.setattr(server, "call_tool", _exploding)
    server_module._guard_tool_dispatch(server, "demo")  # noqa: SLF001
    handler2 = server._mcp_server.request_handlers[_mcp_types.CallToolRequest]  # noqa: SLF001
    result = await handler2(
        _mcp_types.CallToolRequest(
            method="tools/call",
            params=_mcp_types.CallToolRequestParams(
                name="semantic_search", arguments={"query": "票價多少"}
            ),
        )
    )
    backend = cast(_mcp_types.CallToolResult, result.root)
    # it still FAILS — but as the SDK's own generic tool error (content only,
    # no structuredContent), never as our argument refusal
    assert backend.isError is True
    assert backend.structuredContent is None
    assert "INVALID_INPUT" not in cast(_mcp_types.TextContent, backend.content[0]).text


async def test_get_entity_exchanges_a_citation_entity_id_for_content() -> None:
    """A citation's entity UUID must resolve, like get_chunk/get_document.

    §16 surfaces entity UUIDs both as the ``SourceRef(source_type="entity",
    id=…)`` a global_summary community report cites and as the entity result
    ids semantic_search/graph_query return — and the
    initialize instructions promise get_entity/get_chunk/get_document
    "exchange ids from citations for full content". get_entity looked up
    canonical NAMES only, so an entity citation dead-ended everywhere:
    get_chunk rejects an entity id, list_entities substring-matches names,
    and the id resolved nowhere (#153). A cited answer whose citations cannot
    be fetched is unverifiable, which is the whole point of require_sources.

    Name lookup still runs FIRST, so this can only turn a NOT_FOUND into a
    hit — never repoint a name that already resolved.
    """
    import uuid as _uuid

    from core.mcp.server import _get_entity

    cited_id = _uuid.uuid4()

    class _Repo:
        build_id = _uuid.uuid4()

        def __init__(self, *, active: set[Any]) -> None:
            self._active = active
            self.name_lookups: list[str] = []

        async def entity_ids_by_name(self, name: str) -> list[Any]:
            self.name_lookups.append(name)
            return []  # nothing is named by a UUID string

        async def active_entity_ids(self, ids: Any) -> set[Any]:
            return {i for i in ids if i in self._active}

        async def mentions_by_entity(self, ids: Any) -> dict[Any, Any]:
            return {}

        async def chunks_by_content_ref(self, pairs: Any) -> dict[Any, Any]:
            return {}

    repo = _Repo(active={cited_id})
    hit = await _get_entity(cast(Any, repo), "demo", str(cited_id))
    assert hit["error_code"] is None, "a live citation id must resolve, not 404"
    assert [e["id"] for e in hit["entities"]] == [str(cited_id)]
    assert repo.name_lookups == [str(cited_id)], "the NAME lookup must still go first"

    # a UUID that is not an ACTIVE entity in this build stays NOT_FOUND — the
    # id path inherits the name path's drift rule rather than inventing a hit
    stale = await _get_entity(cast(Any, _Repo(active=set())), "demo", str(_uuid.uuid4()))
    assert stale["error_code"] == "NOT_FOUND"

    # a non-UUID miss must not change: it never reaches the id path at all
    # (the _Repo above has no active_entity_ids call to make for it)
    plain = await _get_entity(cast(Any, _Repo(active={cited_id})), "demo", "Nobody")
    assert plain["error_code"] == "NOT_FOUND"
