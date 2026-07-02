"""Contract tests — validate frozen schemas in contracts/ (Track 0, DR-002).

These tests encode the freeze itself: the enums, envelopes and conventions of
DESIGN §15/§27.2 are additive-only, so any drift here is a conscious contract
change (bump the contract version + record it in DESIGN §26), never an accident.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate as validate_openapi

pytestmark = pytest.mark.contract

_CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
_MCP_SCHEMA = _CONTRACTS / "mcp_response.schema.json"
_OPENAPI = _CONTRACTS / "openapi.yaml"

# DESIGN §27.2 — frozen enums. Additive-only: extending them means updating
# DESIGN §27.2 and this list in the same change; removals/renames are breaking.
_FROZEN_ERROR_CODES = frozenset(
    {
        "PROJECT_NOT_FOUND",
        "BUILD_NOT_FOUND",
        "BUILD_NOT_READY",
        "NO_ACTIVE_BUILD",
        "VALIDATION_ERROR",
        "JOB_NOT_FOUND",
        "JOB_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
        "QUERY_UNSAFE",
        "QUERY_TIMEOUT",
        "STORE_UNAVAILABLE",
        "RATE_LIMITED",
        "INTERNAL",
    }
)
_FROZEN_WARNING_CODES = frozenset(
    {
        "STORE_UNAVAILABLE",
        "MODE_SKIPPED",
        "PARTIAL_RESULTS",
        "LOW_CONFIDENCE",
        "GUARDRAIL_BLOCKED",
        "TRUNCATED",
    }
)

# DESIGN §15 — the frozen endpoint surface. Adding a path is additive (update
# here); removing or renaming one is a breaking contract change.
_FROZEN_PATHS = frozenset(
    {
        "/projects",
        "/projects/{project}",
        "/projects/{project}/sources",
        "/projects/{project}/ingest",
        "/projects/{project}/build",
        "/projects/{project}/builds",
        "/projects/{project}/builds/{build_id}",
        "/projects/{project}/builds/{build_id}/activate",
        "/projects/{project}/builds/{build_id}/rollback",
        "/jobs/{job_id}",
        "/jobs/{job_id}/cancel",
        "/jobs/{job_id}/events",
        "/projects/{project}/documents",
        "/projects/{project}/documents/{document_id}",
        "/projects/{project}/chunks",
        "/projects/{project}/chunks/{chunk_id}",
        "/projects/{project}/entities",
        "/projects/{project}/entities/{entity_id}",
        "/projects/{project}/relations",
        "/projects/{project}/relations/{relation_id}",
        "/projects/{project}/graph/subgraph",
        "/projects/{project}/merge-candidates",
        "/projects/{project}/merge-candidates/{candidate_id}/approve",
        "/projects/{project}/merge-candidates/{candidate_id}/reject",
        "/projects/{project}/merge-candidates/{candidate_id}/defer",
        "/projects/{project}/query/semantic",
        "/projects/{project}/query/graph",
        "/projects/{project}/query/sql",
        "/projects/{project}/query/global",
        "/projects/{project}/query/hybrid",
        "/projects/{project}/health",
        "/projects/{project}/metrics",
        "/projects/{project}/eval",
    }
)

_LIST_PATHS = frozenset(
    {
        "/projects",
        "/projects/{project}/sources",
        "/projects/{project}/builds",
        "/projects/{project}/documents",
        "/projects/{project}/chunks",
        "/projects/{project}/entities",
        "/projects/{project}/relations",
        "/projects/{project}/merge-candidates",
    }
)

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    assert _OPENAPI.exists(), "contracts/openapi.yaml is the frozen Track 0 P0 deliverable"
    return cast(dict[str, Any], yaml.safe_load(_OPENAPI.read_text(encoding="utf-8")))


def _deref(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref`` pointers (``#/components/...``)."""
    while "$ref" in node:
        ref = node["$ref"]
        assert isinstance(ref, str) and ref.startswith("#/"), f"non-local $ref: {ref!r}"
        cur: Any = spec
        for part in ref[2:].split("/"):
            cur = cur[part.replace("~1", "/").replace("~0", "~")]
        node = cast(dict[str, Any], cur)
    return node


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in _HTTP_METHODS:
                yield path, method, cast(dict[str, Any], op)


def _param_refs(op: dict[str, Any]) -> set[str]:
    return {
        p["$ref"].rsplit("/", 1)[-1]
        for p in op.get("parameters", [])
        if isinstance(p, dict) and "$ref" in p
    }


def _json_body_schema(spec: dict[str, Any], response: dict[str, Any]) -> dict[str, Any] | None:
    resolved = _deref(spec, response)
    content = resolved.get("content", {})
    if "application/json" not in content:
        return None  # 204 / SSE responses carry no JSON envelope
    return _deref(spec, cast(dict[str, Any], content["application/json"]["schema"]))


def test_mcp_response_schema_is_valid() -> None:
    if not _MCP_SCHEMA.exists():
        pytest.skip("contract not frozen yet (Track 0 P1)")
    schema = json.loads(_MCP_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)


def test_openapi_document_is_valid(spec: dict[str, Any]) -> None:
    """The frozen deliverable must be a structurally valid OpenAPI document."""
    validate_openapi(spec)


def test_contract_is_versioned(spec: dict[str, Any]) -> None:
    """DR-002: the contract is a versioned deliverable; breaking changes bump it."""
    assert spec["info"]["version"] == "1.0"


def test_frozen_endpoint_surface(spec: dict[str, Any]) -> None:
    """§15 endpoint list is the contract surface core/api/web all code against."""
    assert set(spec["paths"]) == _FROZEN_PATHS


def test_error_code_enum_is_frozen(spec: dict[str, Any]) -> None:
    codes = spec["components"]["schemas"]["ErrorCode"]["enum"]
    assert len(codes) == len(set(codes)), "duplicate error codes"
    assert set(codes) == _FROZEN_ERROR_CODES


def test_warning_code_enum_is_frozen(spec: dict[str, Any]) -> None:
    codes = spec["components"]["schemas"]["WarningCode"]["enum"]
    assert len(codes) == len(set(codes)), "duplicate warning codes"
    assert set(codes) == _FROZEN_WARNING_CODES


def test_every_success_body_uses_the_envelope(spec: dict[str, Any]) -> None:
    """§15: every JSON success body is {data, meta{request_id, build_id, elapsed_ms}}
    so clients (and the FE codegen) can rely on one shape everywhere."""
    checked = 0
    for path, method, op in _operations(spec):
        for status, response in op["responses"].items():
            if not str(status).startswith("2"):
                continue
            schema = _json_body_schema(spec, cast(dict[str, Any], response))
            if schema is None:
                continue
            where = f"{method.upper()} {path} {status}"
            assert set(schema.get("required", [])) >= {"data", "meta"}, where
            meta = _deref(spec, cast(dict[str, Any], schema["properties"]["meta"]))
            assert set(meta["properties"]) >= {"request_id", "build_id", "elapsed_ms"}, where
            assert set(meta["required"]) >= {"request_id", "build_id", "elapsed_ms"}, where
            checked += 1
    assert checked >= len(_FROZEN_PATHS)  # guard: the loop actually saw the surface


def test_every_error_body_uses_the_error_envelope(spec: dict[str, Any]) -> None:
    """§15: every operation documents the error envelope with a frozen code enum,
    so clients never have to parse ad-hoc error shapes."""
    for path, method, op in _operations(spec):
        where = f"{method.upper()} {path}"
        assert "default" in op["responses"], f"{where} lacks a default error response"
        for status, response in op["responses"].items():
            if str(status).startswith("2"):
                continue
            schema = _json_body_schema(spec, cast(dict[str, Any], response))
            assert schema is not None, f"{where} {status}"
            assert schema.get("required") == ["error"], f"{where} {status}"
            error = _deref(spec, cast(dict[str, Any], schema["properties"]["error"]))
            # The whole frozen shape is required — details is null rather than
            # absent, so error consumers never branch on missing fields.
            assert set(error["required"]) == {"code", "message", "details", "request_id"}, where
            code = _deref(spec, cast(dict[str, Any], error["properties"]["code"]))
            assert set(code["enum"]) == _FROZEN_ERROR_CODES, where


def test_list_endpoints_use_cursor_pagination(spec: dict[str, Any]) -> None:
    """§15: pagination is cursor-based (?limit=&cursor= → meta.next_cursor) on every
    collection endpoint — offset pagination is not part of the contract."""
    seen: set[str] = set()
    for path, method, op in _operations(spec):
        if method != "get" or "200" not in op["responses"]:
            continue
        schema = _json_body_schema(spec, cast(dict[str, Any], op["responses"]["200"]))
        if schema is None:
            continue
        data = _deref(spec, cast(dict[str, Any], schema["properties"]["data"]))
        if data.get("type") != "array":
            continue
        seen.add(path)
        refs = _param_refs(op)
        assert {"Limit", "Cursor", "Sort", "Filter"} <= refs, path
        meta = _deref(spec, cast(dict[str, Any], schema["properties"]["meta"]))
        # next_cursor is required (null on the last page) so clients can always
        # distinguish "no more pages" from a non-conforming response.
        assert "next_cursor" in meta["properties"], path
        assert "next_cursor" in meta["required"], path
    assert seen == _LIST_PATHS


def test_write_endpoints_accept_idempotency_key(spec: dict[str, Any]) -> None:
    """§15/§27.2: retried POSTs must not double-trigger builds/ingests — every write
    POST takes Idempotency-Key and documents 409 IDEMPOTENCY_CONFLICT. Query
    endpoints are read-only RPC and take no key."""
    writes: set[str] = set()
    for path, method, op in _operations(spec):
        if method != "post":
            continue
        refs = _param_refs(op)
        if "/query/" in path:
            assert "IdempotencyKey" not in refs, path
            continue
        writes.add(path)
        assert "IdempotencyKey" in refs, path
        assert "409" in op["responses"], path
    assert len(writes) == 10  # create/source/ingest/build/activate/rollback/cancel + 3 reviews


def test_sse_job_event_contract(spec: dict[str, Any]) -> None:
    """§27.2 freeze: event names job.update|job.done|job.failed and the JobEvent
    data payload — the Console's live progress UI is coded against exactly this."""
    op = spec["paths"]["/jobs/{job_id}/events"]["get"]
    assert op["x-sse-events"] == ["job.update", "job.done", "job.failed"]
    schema = _deref(
        spec,
        cast(dict[str, Any], op["responses"]["200"]["content"]["text/event-stream"]["schema"]),
    )
    assert set(schema["properties"]) == {"job_id", "status", "step", "progress", "message", "ts"}
    # The whole frozen shape is required — step/message are null rather than absent,
    # so SSE consumers never branch on missing fields.
    assert set(schema["required"]) == set(schema["properties"])
    progress = schema["properties"]["progress"]
    assert progress["minimum"] == 0 and progress["maximum"] == 1
    status = _deref(spec, cast(dict[str, Any], schema["properties"]["status"]))
    assert set(status["enum"]) == {"queued", "running", "done", "failed", "cancelled"}


def test_retrieval_results_must_cite_sources(spec: dict[str, Any]) -> None:
    """§16/§27.2 require_sources: an answer nobody can trace to a source is worthless
    — every retrieval result carries at least one source_ref, structurally."""
    result = spec["components"]["schemas"]["RetrievalResult"]
    assert "source_refs" in result["required"]
    refs = result["properties"]["source_refs"]
    assert refs["minItems"] == 1
    source_ref = _deref(spec, cast(dict[str, Any], refs["items"]))
    assert set(source_ref["required"]) >= {"source_type", "id"}


def test_query_result_is_build_scoped_and_warns_typed(spec: dict[str, Any]) -> None:
    """DR-001/§22: every query response names the build it read (no silent version
    mixing) and degradation is reported through typed warnings, not failures."""
    result = spec["components"]["schemas"]["QueryResult"]
    assert set(result["required"]) >= {"mode", "build_id", "results", "warnings"}
    warning_items = _deref(spec, cast(dict[str, Any], result["properties"]["warnings"]["items"]))
    code = _deref(spec, cast(dict[str, Any], warning_items["properties"]["code"]))
    assert set(code["enum"]) == _FROZEN_WARNING_CODES
    # debug is typed (not free-form) so generated clients can rely on
    # debug.routing_decision when query_policy.expose_debug allows it.
    debug = _deref(spec, cast(dict[str, Any], result["properties"]["debug"]["anyOf"][0]))
    assert set(debug["required"]) == {
        "stores_used",
        "retrieval_plan",
        "routing_decision",
        "latency_ms",
    }
    routing = _deref(
        spec, cast(dict[str, Any], debug["properties"]["routing_decision"]["anyOf"][0])
    )
    assert set(routing["required"]) == {"selected", "skipped"}


def test_auth_placeholder_guards_every_endpoint(spec: dict[str, Any]) -> None:
    """§15/§23: all endpoints sit behind the auth dependency; swapping in real auth
    later must not change the contract."""
    assert spec["security"] == [{"bearerAuth": []}]
    scheme = spec["components"]["securitySchemes"]["bearerAuth"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
    for path, method, op in _operations(spec):
        assert "security" not in op, f"{method.upper()} {path} overrides global security"


def test_operation_ids_are_unique(spec: dict[str, Any]) -> None:
    """FE0 generates the typed client from operationIds — collisions break codegen."""
    ids = [op["operationId"] for _, _, op in _operations(spec)]
    assert len(ids) == len(set(ids))
    assert all(ids)
