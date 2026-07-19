"""Contract tests — validate frozen schemas in contracts/ (Track 0, DR-002).

These tests encode the freeze itself: the enums, envelopes and conventions of
DESIGN §15/§27.2 are additive-only, so any drift here is a conscious contract
change (bump the contract version + record it in DESIGN §26), never an accident.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

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
        # v1.3 (DR-013, additive): precise not-found codes for the new
        # source/proposal surfaces + the retry state refusal.
        "SOURCE_NOT_FOUND",
        "PROPOSAL_NOT_FOUND",
        "BUILD_NOT_RETRYABLE",
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
        "/projects/{project}/uploads",
        "/projects/{project}/build",
        "/projects/{project}/builds",
        "/projects/{project}/builds/{build_id}",
        "/projects/{project}/builds/{build_id}/activate",
        "/projects/{project}/builds/{build_id}/rollback",
        "/projects/{project}/builds/{build_id}/eval",
        # v1.3 (DR-013 packaged round)
        "/projects/{project}/sources/{source_id}",
        "/projects/{project}/builds/{build_id}/retry",
        "/projects/{project}/builds/{build_id}/steps",
        "/projects/{project}/builds/{build_id}/steps/{step_id}/items",
        "/projects/{project}/entities/{entity_id}/approve",
        "/projects/{project}/entities/{entity_id}/reject",
        "/projects/{project}/relations/{relation_id}/approve",
        "/projects/{project}/relations/{relation_id}/reject",
        "/projects/{project}/ontology-proposals",
        "/projects/{project}/ontology-proposals/{proposal_id}/accept",
        "/projects/{project}/ontology-proposals/{proposal_id}/reject",
        "/projects/{project}/mcp",
        "/jobs/{job_id}",
        "/jobs/{job_id}/cancel",
        "/jobs/{job_id}/events",
        "/projects/{project}/documents",
        "/projects/{project}/documents/{document_id}",
        "/projects/{project}/chunks",
        "/projects/{project}/chunks/{chunk_id}",
        "/projects/{project}/clean/preview",
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
        # v1.3 (DR-013)
        "/projects/{project}/ontology-proposals",
        "/projects/{project}/builds/{build_id}/steps",
        "/projects/{project}/builds/{build_id}/steps/{step_id}/items",
    }
)

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    assert _OPENAPI.exists(), "contracts/openapi.yaml is the frozen Track 0 P0 deliverable"
    return cast(dict[str, Any], yaml.safe_load(_OPENAPI.read_text(encoding="utf-8")))


def _pointer(spec: dict[str, Any], ref: str) -> Any:
    """Follow one local JSON Pointer (``#/a/b/c``) to its target.

    The fragment is a URI: percent-decode each token FIRST (``display%20name``
    → ``display name``), then apply the JSON Pointer ``~1``/``~0`` transforms —
    that order is the RFC 6901 evaluation order.
    """
    assert isinstance(ref, str) and ref.startswith("#/"), f"non-local $ref: {ref!r}"
    cur: Any = spec
    for part in ref[2:].split("/"):
        cur = cur[unquote(part).replace("~1", "/").replace("~0", "~")]
    return cur


def _deref(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref`` pointers (``#/components/...``)."""
    while "$ref" in node:
        node = cast(dict[str, Any], _pointer(spec, node["$ref"]))
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


@pytest.fixture(scope="module")
def mcp_schema() -> dict[str, Any]:
    assert _MCP_SCHEMA.exists(), (
        "contracts/mcp_response.schema.json is the frozen Track 0 P1 deliverable"
    )
    return cast(dict[str, Any], json.loads(_MCP_SCHEMA.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def mcp_validator(mcp_schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    # format_checker makes "format": "uuid" enforcing — without it the build_id
    # scoping guarantee (DR-001) would be decorative.
    return jsonschema.Draft202012Validator(
        mcp_schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )


def test_mcp_response_schema_is_valid(mcp_schema: dict[str, Any]) -> None:
    """The frozen deliverable must be a valid Draft 2020-12 schema."""
    jsonschema.Draft202012Validator.check_schema(mcp_schema)


def test_mcp_schema_version_is_frozen(mcp_schema: dict[str, Any]) -> None:
    """DR-002: schema_version pins the contract; only a breaking change bumps it."""
    assert mcp_schema["properties"]["schema_version"]["const"] == "1.0"
    assert "schema_version" in mcp_schema["required"]


def test_mcp_response_is_build_scoped(mcp_schema: dict[str, Any]) -> None:
    """DR-001: every MCP answer names the build it read — build_id is required, so
    old-version data can never sneak into a response unnoticed."""
    assert "build_id" in mcp_schema["required"]


def test_mcp_enums_stay_in_lockstep_with_openapi(
    spec: dict[str, Any], mcp_schema: dict[str, Any]
) -> None:
    """The Console playground (openapi.yaml) and the MCP tools expose the same
    retrieval surface — enum drift between the two frozen artifacts would fork
    the contract for web vs agent consumers."""
    api = spec["components"]["schemas"]
    defs = mcp_schema["$defs"]
    assert set(defs["WarningCode"]["enum"]) == set(api["WarningCode"]["enum"])
    assert set(defs["WarningCode"]["enum"]) == _FROZEN_WARNING_CODES
    result_type = defs["RetrievalResult"]["properties"]["result_type"]
    assert set(result_type["enum"]) == set(api["ResultType"]["enum"])
    assert set(defs["SourceRefType"]["enum"]) == set(api["SourceRefType"]["enum"])
    assert set(defs["QueryMode"]["enum"]) == set(api["QueryMode"]["enum"])


def _valid_mcp_response() -> dict[str, Any]:
    """A §16-shaped response exercising all six result_types and their §27.2 minimums."""
    n1 = "0a4b1d2e-3f40-4a51-8b62-c73d84e95fa6"
    n2 = "1b5c2e3f-4a51-4b62-9c73-d84e95fa6b07"
    e1 = "2c6d3f4a-5b62-4c73-ad84-e95fa6b07c18"
    chunk_id = "3d7e4a5b-6c73-4d84-be95-fa6b07c18d29"
    chunk_uri = "s3://acme/docs/onboarding.md"
    return {
        "schema_version": "1.0",
        "query": "who owns onboarding?",
        "tool": "hybrid_query",
        "project": "acme",
        "build_id": "7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d",
        "results": [
            {
                "result_type": "chunk",
                "id": chunk_id,
                "title": "onboarding.md#3",
                "text": "People Ops owns onboarding.",
                "score": 0.93,
                "confidence": 0.9,
                "source_refs": [
                    {
                        "source_type": "chunk",
                        "id": chunk_id,
                        "source_uri": chunk_uri,
                        "metadata": {"start_offset": 1204, "end_offset": 1490},
                    }
                ],
            },
            {
                "result_type": "entity",
                "id": n1,
                "title": "People Ops",
                "text": None,
                "score": 0.88,
                "confidence": 0.95,
                "source_refs": [{"source_type": "chunk", "id": chunk_id, "source_uri": chunk_uri}],
            },
            {
                "result_type": "relation",
                "id": e1,
                "title": "People Ops -[OWNS]-> Onboarding",
                "text": None,
                "score": 0.85,
                "confidence": 0.8,
                "source_refs": [
                    {
                        "source_type": "chunk",
                        "id": chunk_id,
                        "source_uri": chunk_uri,
                        "metadata": {
                            "quote": "People Ops owns onboarding.",
                            "start_offset": 1204,
                            "end_offset": 1231,
                        },
                    }
                ],
            },
            {
                "result_type": "path",
                "id": "path-1",
                "title": None,
                "text": None,
                "score": 0.8,
                "confidence": None,
                "source_refs": [{"source_type": "relation", "id": e1}],
            },
            {
                "result_type": "row",
                "id": "employees:42",
                "title": None,
                "text": None,
                "score": 0.7,
                "confidence": None,
                "source_refs": [
                    {
                        "source_type": "row",
                        "id": "42",
                        "metadata": {"table": "employees", "pk": "42"},
                    }
                ],
            },
            {
                "result_type": "community_report",
                "id": "5f9a6b7c-8d95-4ea6-bf07-a1b2c3d4e5f6",
                "title": "HR cluster",
                "text": "Summary of the HR-related community.",
                "score": 0.6,
                "confidence": 0.7,
                "source_refs": [{"source_type": "entity", "id": n1}],
            },
        ],
        "graph_context": {
            "nodes": [
                {"id": n1, "type": "Team", "label": "People Ops", "properties": {}},
                {"id": n2, "type": "Process", "label": "Onboarding", "properties": {}},
            ],
            "edges": [{"id": e1, "src": n1, "dst": n2, "type": "OWNS", "confidence": 0.8}],
            "paths": [{"nodes": [n1, n2], "edges": [e1]}],
        },
        "warnings": [{"code": "MODE_SKIPPED", "message": "sql skipped: low router confidence"}],
        "debug": {
            "stores_used": ["qdrant", "neo4j"],
            "retrieval_plan": ["semantic", "graph"],
            "routing_decision": {
                "selected": ["semantic", "graph"],
                "skipped": ["sql", "global"],
                "reason": "entity-centric question",
                "confidence": 0.83,
            },
            "latency_ms": 412,
        },
    }


def test_mcp_valid_response_passes(mcp_validator: jsonschema.Draft202012Validator) -> None:
    """The canonical §16 payload (all six result_types, each meeting its §27.2
    source_refs minimum) must validate — otherwise the schema is stricter than
    the design and would reject conforming servers."""
    mcp_validator.validate(_valid_mcp_response())


def test_mcp_relation_document_evidence_needs_no_offsets(
    mcp_validator: jsonschema.Draft202012Validator,
) -> None:
    """§4 evidence_type includes `manual`: document-level citations have no source
    span, so only chunk-derived relation evidence must carry offsets (§27.4).
    Requiring offsets on document refs would make manual evidence unrepresentable."""
    payload = _valid_mcp_response()
    payload["results"][2]["source_refs"] = [
        {
            "source_type": "document",
            "id": "d1",
            "source_uri": "s3://acme/docs/onboarding.md",
            "metadata": {"quote": "People Ops owns onboarding."},
        }
    ]
    mcp_validator.validate(payload)


def _drop_result_source_refs(p: dict[str, Any]) -> None:
    del p["results"][0]["source_refs"]


def _empty_source_refs(p: dict[str, Any]) -> None:
    p["results"][0]["source_refs"] = []


def _chunk_ref_without_offsets(p: dict[str, Any]) -> None:
    del p["results"][0]["source_refs"][0]["metadata"]["start_offset"]


def _chunk_ref_with_non_numeric_offsets(p: dict[str, Any]) -> None:
    p["results"][0]["source_refs"][0]["metadata"] = {"start_offset": None, "end_offset": "abc"}


def _chunk_ref_with_negative_offset(p: dict[str, Any]) -> None:
    p["results"][0]["source_refs"][0]["metadata"]["start_offset"] = -1


def _chunk_ref_without_uri(p: dict[str, Any]) -> None:
    del p["results"][0]["source_refs"][0]["source_uri"]


def _entity_without_mention(p: dict[str, Any]) -> None:
    p["results"][1]["source_refs"] = [{"source_type": "entity", "id": "self"}]


def _relation_without_evidence(p: dict[str, Any]) -> None:
    p["results"][2]["source_refs"] = [{"source_type": "relation", "id": "self"}]


def _relation_ref_bare_document(p: dict[str, Any]) -> None:
    p["results"][2]["source_refs"] = [{"source_type": "document", "id": "d"}]


def _relation_ref_without_quote(p: dict[str, Any]) -> None:
    del p["results"][2]["source_refs"][0]["metadata"]["quote"]


def _relation_row_ref_without_pk(p: dict[str, Any]) -> None:
    p["results"][2]["source_refs"] = [
        {"source_type": "row", "id": "42", "metadata": {"table": "employees"}}
    ]


def _relation_chunk_ref_without_offsets(p: dict[str, Any]) -> None:
    del p["results"][2]["source_refs"][0]["metadata"]["start_offset"]


def _relation_quote_over_512(p: dict[str, Any]) -> None:
    p["results"][2]["source_refs"][0]["metadata"]["quote"] = "x" * 513


def _relation_document_quote_over_512(p: dict[str, Any]) -> None:
    p["results"][2]["source_refs"] = [
        {
            "source_type": "document",
            "id": "d1",
            "source_uri": "s3://acme/docs/onboarding.md",
            "metadata": {"quote": "x" * 513},
        }
    ]


def _chunk_ref_with_empty_uri(p: dict[str, Any]) -> None:
    p["results"][0]["source_refs"][0]["source_uri"] = ""


def _path_without_relation_ref(p: dict[str, Any]) -> None:
    p["results"][3]["source_refs"] = [{"source_type": "chunk", "id": "c"}]


def _row_without_table_pk(p: dict[str, Any]) -> None:
    del p["results"][4]["source_refs"][0]["metadata"]["table"]


def _row_with_null_table_pk(p: dict[str, Any]) -> None:
    p["results"][4]["source_refs"][0]["metadata"] = {"table": None, "pk": None}


def _row_with_empty_table(p: dict[str, Any]) -> None:
    p["results"][4]["source_refs"][0]["metadata"]["table"] = ""


def _row_with_empty_pk(p: dict[str, Any]) -> None:
    p["results"][4]["source_refs"][0]["metadata"]["pk"] = ""


def _report_without_member_entities(p: dict[str, Any]) -> None:
    p["results"][5]["source_refs"] = [{"source_type": "document", "id": "d"}]


def _source_ref_with_empty_id(p: dict[str, Any]) -> None:
    p["results"][5]["source_refs"][0]["id"] = ""


def _result_with_empty_id(p: dict[str, Any]) -> None:
    p["results"][0]["id"] = ""


def _empty_project(p: dict[str, Any]) -> None:
    p["project"] = ""


def _unknown_warning_code(p: dict[str, Any]) -> None:
    p["warnings"][0]["code"] = "SOMETHING_ELSE"


def _wrong_schema_version(p: dict[str, Any]) -> None:
    p["schema_version"] = "2.0"


def _missing_build_id(p: dict[str, Any]) -> None:
    del p["build_id"]


def _non_retrieval_tool(p: dict[str, Any]) -> None:
    p["tool"] = "list_schema"


def _confidence_out_of_range(p: dict[str, Any]) -> None:
    p["results"][0]["confidence"] = 1.5


def _malformed_build_id(p: dict[str, Any]) -> None:
    p["build_id"] = "not-a-uuid"


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_result_source_refs,
        _empty_source_refs,
        _chunk_ref_without_offsets,
        _chunk_ref_with_non_numeric_offsets,
        _chunk_ref_with_negative_offset,
        _chunk_ref_without_uri,
        _entity_without_mention,
        _relation_without_evidence,
        _relation_ref_bare_document,
        _relation_ref_without_quote,
        _relation_row_ref_without_pk,
        _relation_chunk_ref_without_offsets,
        _relation_quote_over_512,
        _relation_document_quote_over_512,
        _chunk_ref_with_empty_uri,
        _path_without_relation_ref,
        _row_without_table_pk,
        _row_with_null_table_pk,
        _row_with_empty_table,
        _row_with_empty_pk,
        _report_without_member_entities,
        _source_ref_with_empty_id,
        _result_with_empty_id,
        _empty_project,
        _unknown_warning_code,
        _wrong_schema_version,
        _missing_build_id,
        _non_retrieval_tool,
        _confidence_out_of_range,
        _malformed_build_id,
    ],
    ids=lambda f: f.__name__.lstrip("_"),
)
def test_mcp_schema_rejects_contract_violations(
    mcp_validator: jsonschema.Draft202012Validator,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """require_sources and the §27.2 per-result_type minimums must *bite*: an
    answer nobody can trace (or a payload outside the frozen enums/version) is
    rejected by the schema, not silently accepted."""
    payload = copy.deepcopy(_valid_mcp_response())
    mutate(payload)
    with pytest.raises(jsonschema.ValidationError):
        mcp_validator.validate(payload)


def test_openapi_document_is_valid(spec: dict[str, Any]) -> None:
    """The frozen deliverable must be a structurally valid OpenAPI document."""
    validate_openapi(spec)


def test_contract_is_versioned(spec: dict[str, Any]) -> None:
    """DR-002: the contract is a versioned deliverable; changes bump it.

    1.0 → 1.1 (2026-07-13, DESIGN §26 DR-009): added POST
    /projects/{project}/clean/preview — the §10.2 抽樣預覽 endpoint.
    1.1 → 1.2 (2026-07-15, DESIGN §26 DR-010): added POST
    /projects/{project}/builds/{build_id}/eval + POST /projects/{project}/uploads
    and the document metadata envelope.
    1.2 → 1.3 (2026-07-18, DESIGN §26 DR-013): the Track 5 packaged round —
    source soft-disable (SRC2), entity/relation + ontology-proposal review
    (GOV2/GOV3), build retry lineage + step/item drill-down (RB1), server-side
    search + page totals (SS1b), and the MCP connection-info endpoint (DR-012
    rider).
    """
    assert spec["info"]["version"] == "1.3"


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
    # Read-only RPC over POST: no side effects, so nothing to replay — a key here
    # would falsely signal write semantics. Queries (§15) and the v1.1 clean
    # preview (DR-009: a pure function, nothing persisted) are this category.
    read_rpc = ("/query/", "/clean/preview")
    for path, method, op in _operations(spec):
        if method != "post":
            continue
        refs = _param_refs(op)
        if any(marker in path for marker in read_rpc):
            assert "IdempotencyKey" not in refs, path
            continue
        writes.add(path)
        assert "IdempotencyKey" in refs, path
        assert "409" in op["responses"], path
    # v1.2 (DR-010): create/source/ingest/upload/build/activate/rollback/eval/
    # cancel + 3 merge reviews = 12. v1.3 (DR-013) adds 7: entity approve/
    # reject, relation approve/reject, proposal accept/reject, build retry.
    assert len(writes) == 19


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


def test_build_eval_endpoint_contract(spec: dict[str, Any]) -> None:
    """v1.2/DR-010: a Console-triggered eval is an async job over a NAMED build —
    202 + the job envelope (SSE-watchable), Idempotency-Key + 409, and NO request
    body (the golden set is the project's configured one, the build is in the path).
    Without this endpoint the eval that activation gating reads is CLI-only."""
    op = spec["paths"]["/projects/{project}/builds/{build_id}/eval"]["post"]
    assert op["operationId"] == "runBuildEval"
    assert "requestBody" not in op
    assert _param_refs(op) == {"IdempotencyKey"}
    assert "409" in op["responses"]
    accepted = _json_body_schema(spec, cast(dict[str, Any], op["responses"]["202"]))
    assert accepted is not None and set(accepted["required"]) >= {"data", "meta"}
    data = _deref(spec, cast(dict[str, Any], accepted["properties"]["data"]))
    assert set(data["required"]) >= {"job_id", "status"}


def test_upload_endpoint_contract(spec: dict[str, Any]) -> None:
    """v1.2/DR-010: upload is a multipart write that registers/updates a managed
    source and returns an honest per-file manifest. Request-level policy breaches
    are loud (413 total-size, 415 wrong media type); a rejected file is a STATED
    refusal, never a silent drop. Per-file metadata is context/governance ONLY —
    the system namespace is not client-writable (rule 1/4)."""
    op = spec["paths"]["/projects/{project}/uploads"]["post"]
    assert op["operationId"] == "uploadDocuments"
    assert _param_refs(op) == {"IdempotencyKey"}
    for status in ("409", "413", "415"):
        assert status in op["responses"], status
    body = op["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert "files" in body["required"]
    meta_input = _deref(
        spec, cast(dict[str, Any], body["properties"]["metadata"]["additionalProperties"])
    )
    assert meta_input["additionalProperties"] is False
    assert set(meta_input["properties"]) == {"context", "governance"}
    result = _json_body_schema(spec, cast(dict[str, Any], op["responses"]["201"]))
    assert result is not None and set(result["required"]) >= {"data", "meta"}
    data = _deref(spec, cast(dict[str, Any], result["properties"]["data"]))
    assert set(data["required"]) == {"source_id", "files"}
    uploaded = _deref(spec, cast(dict[str, Any], data["properties"]["files"]["items"]))
    variants = [_deref(spec, cast(dict[str, Any], v)) for v in uploaded["oneOf"]]
    by_status = {v["properties"]["status"]["enum"][0]: v for v in variants}
    assert set(by_status) == {"accepted", "rejected"}
    # a rejected file MUST carry a non-empty reason and no uri — the "stated
    # refusal, never a silent drop" guarantee is structural, not optional (Codex
    # PR#80 P2). The accepted variant carries the canonical uri and forbids reason.
    rejected = by_status["rejected"]
    assert "reason" in rejected["required"]
    assert rejected["properties"]["reason"]["minLength"] == 1
    assert rejected["properties"]["document_uri"] is False
    assert rejected["properties"]["filename"] is False  # nothing stored for a reject
    accepted = by_status["accepted"]
    assert "document_uri" in accepted["required"]
    assert accepted["properties"]["reason"] is False
    # the submitted filename is the client's matching key back to its selection and
    # to the (filename-keyed) upload metadata — required + non-null on BOTH variants
    # so every manifest row is correlatable (Codex PR#80)
    for variant in (accepted, rejected):
        assert "original_filename" in variant["required"]
        assert variant["properties"]["original_filename"]["type"] == "string"
    # the manifest is non-empty for a non-empty upload — no silent all-drop (Codex PR#80)
    assert data["properties"]["files"]["minItems"] == 1


def test_document_metadata_envelope_shape(spec: dict[str, Any]) -> None:
    """v1.2/DR-010: the metadata envelope has three namespaces with fixed roles —
    system (server-owned), context (project-defined), governance (exposure-gated).
    The client-facing input excludes system/schema_version STRUCTURALLY, so human
    input can never overwrite or forge connector/system fields (rule 1/4); context
    is a stable shape (not a global field enum) so it stays domain-agnostic."""
    schemas = spec["components"]["schemas"]
    envelope = schemas["DocumentMetadataEnvelope"]
    assert set(envelope["required"]) == {"schema_version", "system", "context", "governance"}
    context = schemas["DocumentMetadataContext"]
    assert context["additionalProperties"] is False
    assert context["properties"]["attributes"]["additionalProperties"] is True
    metadata_input = schemas["DocumentMetadataInput"]
    assert metadata_input["additionalProperties"] is False
    assert "system" not in metadata_input["properties"]
    assert "schema_version" not in metadata_input["properties"]
    assert set(metadata_input["properties"]) == {"context", "governance"}
    # the envelope is load-bearing, not a dead type: the accepted-upload response
    # carries the STORED envelope, so the server-stamped system/schema_version
    # can't be silently dropped (Codex PR#80 P1). The full envelope requires them.
    accepted = schemas["UploadedFileAccepted"]
    assert "metadata" in accepted["required"]
    assert accepted["properties"]["metadata"]["$ref"].endswith("/DocumentMetadataEnvelope")


def test_v13_source_lifecycle_contract(spec: dict[str, Any]) -> None:
    """DR-013/SRC2 (GAPS G2 option 2): the ONLY mutable source field is
    `enabled` — `uri`/`kind` immutability is structural (not properties of the
    update schema), so a client cannot even EXPRESS a uri rewrite; corpus swap
    is disable-old + register-new and historical provenance survives."""
    defs = spec["components"]["schemas"]
    update = defs["SourceUpdate"]
    assert update["required"] == ["enabled"]  # an empty PATCH body is a client bug
    assert set(update["properties"]) == {"enabled"}  # uri/kind not expressible
    # ...and a smuggled extra key is a VALIDATION error at the schema boundary,
    # not a silently-dropped no-op (the DocumentMetadataInput precedent)
    assert update["additionalProperties"] is False
    # the response side gains `enabled` additively: NOT required, so pre-SRC2
    # responses (field absent = enabled) stay conforming until the runtime lands
    source = defs["Source"]
    assert source["properties"]["enabled"]["type"] == "boolean"
    assert "enabled" not in source["required"]
    patch = spec["paths"]["/projects/{project}/sources/{source_id}"]["patch"]
    assert patch["requestBody"]["required"] is True


def test_v13_review_endpoints_share_the_decision_request(spec: dict[str, Any]) -> None:
    """DR-013/GOV2+GOV3: all six new review writes reuse the ONE
    ReviewDecisionRequest body (§17 vocabulary lives in the PATH, mirroring the
    merge-candidate shape — no per-kind body forks to drift), and each returns
    its subject's envelope so the Console can refresh in place."""
    expected = {
        "/projects/{project}/entities/{entity_id}/approve": "EntityResponse",
        "/projects/{project}/entities/{entity_id}/reject": "EntityResponse",
        "/projects/{project}/relations/{relation_id}/approve": "RelationResponse",
        "/projects/{project}/relations/{relation_id}/reject": "RelationResponse",
        "/projects/{project}/ontology-proposals/{proposal_id}/accept": "OntologyProposalResponse",
        "/projects/{project}/ontology-proposals/{proposal_id}/reject": "OntologyProposalResponse",
    }
    for path, wrapper in expected.items():
        op = spec["paths"][path]["post"]
        body = op["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith("ReviewDecisionRequest"), path
        ok = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert ok["$ref"].endswith(wrapper), path


def test_v13_ontology_proposal_enums_match_the_ddl(spec: dict[str, Any]) -> None:
    """DR-013/GOV3: the proposal `kind`/`status` vocabularies are CLOSED in the
    DDL (CheckConstraints) — the contract enums must stay in lockstep with the
    SoR's own constraint text (two gates, one corpus: parse the DDL rather than
    restating it)."""
    import re

    import sqlalchemy as sa

    from core.stores.tables import ontology_proposals

    def ddl_vocab(constraint_name: str) -> set[str]:
        for c in ontology_proposals.constraints:
            if isinstance(c, sa.CheckConstraint) and c.name == constraint_name:
                return set(re.findall(r"'([^']+)'", str(c.sqltext)))
        raise AssertionError(f"constraint {constraint_name} not found")

    defs = spec["components"]["schemas"]
    assert set(defs["OntologyProposal"]["properties"]["kind"]["enum"]) == ddl_vocab(
        "ontology_proposals_kind_valid"
    )
    assert set(defs["OntologyProposalStatus"]["enum"]) == ddl_vocab(
        "ontology_proposals_status_valid"
    )


def test_v13_retry_contract(spec: dict[str, Any]) -> None:
    """DR-013/RB1: retry is a JOB accept (202, same envelope as triggerBuild —
    one watch path for every long operation) whose semantics are fixed
    (failed-only; a full re-run is POST /build), so the body carries only the
    operator note; lineage is the CHILD's nullable parent pointer — the
    parent's terminal record is never mutated (no writable field exists)."""
    defs = spec["components"]["schemas"]
    retry = spec["paths"]["/projects/{project}/builds/{build_id}/retry"]["post"]
    accepted = retry["responses"]["202"]["content"]["application/json"]["schema"]
    assert accepted["$ref"].endswith("JobAcceptedResponse")
    assert set(defs["RetryRequest"]["properties"]) == {"reason"}  # no mode fork
    # closed body: a `{"mode":"all"}` the fixed semantics can't honor is a
    # validation error, not a silently-ignored control (the SourceUpdate
    # precedent — a prose "fixed semantics" promise made structural)
    assert defs["RetryRequest"].get("additionalProperties") is False
    parent = defs["Build"]["properties"]["parent_build_id"]
    assert parent["type"] == ["string", "null"]
    assert "parent_build_id" not in defs["Build"]["required"]  # additive
    # drill-down items carry the STABLE retry key — required, never nullable
    item = defs["BuildStepItem"]
    assert {"item_kind", "item_ref", "status"} <= set(item["required"])


def test_v13_search_and_totals_are_additive(spec: dict[str, Any]) -> None:
    """DR-013/SS1b: `q` is declared on exactly the endpoints whose runtime will
    honor it (an undeclared `q` is the same loud rejection as an unsupported
    filter — never silently ignored), and PageMeta's totals are OPTIONAL and
    nullable so every pre-v1.3 list response stays conforming (null = unknown,
    never zero)."""
    with_q = {
        path
        for path, method, op in _operations(spec)
        if method == "get"
        and any(p.get("$ref", "").endswith("/Q") for p in op.get("parameters", []))
    }
    assert with_q == {"/projects/{project}/documents", "/projects/{project}/entities"}
    meta = spec["components"]["schemas"]["PageMeta"]
    assert meta["properties"]["total"]["type"] == ["integer", "null"]
    assert meta["properties"]["total_estimated"]["type"] == "boolean"
    assert "total" not in meta["required"]
    assert "total_estimated" not in meta["required"]


def test_v13_mcp_info_contract(spec: dict[str, Any]) -> None:
    """DR-013 rider (DR-012's deferred Console surface): the MCP info payload
    is fully closed — transport is const (one gateway shape), auth is the §23
    placeholder enum (additive evolution), and `url` is REQUIRED and non-null.
    The endpoint is path-keyed on `{project}`, so it is only reachable for
    path-addressable projects — the exact set for which a gateway url exists —
    so a null-url / not-addressable branch would be unreachable by
    construction and is deliberately NOT in the payload (Codex #94 R2: don't
    advertise a state the endpoint's own shape can never return)."""
    info = spec["components"]["schemas"]["McpInfo"]
    assert set(info["required"]) == {"transport", "auth", "url"}
    assert info["properties"]["transport"]["const"] == "streamable-http"
    assert info["properties"]["auth"]["enum"] == ["none"]
    assert (
        info["properties"]["url"]["type"] == "string"
    )  # non-null: always reachable ⇒ always a url
    assert "path_addressable" not in info["properties"]  # the unreachable branch is gone


# H20a — class-24 mechanized (#94/#80): a fixed-semantics request body that
# does not declare additionalProperties is SILENTLY open (the JSON-Schema
# default), so a conformant client may send junk keys the contract never
# promised to accept. Every object node in every request body must therefore
# DECLARE its openness: `false` (closed — the norm) or `true` (a documented
# free-form bag). The runtime face already rejects extras on every pinned
# JSON model below (api/schemas.py: extra="forbid") — EXCEPT the multipart
# uploads body, which has no model at all: api/routers/uploads.py reads only
# the `files`/`metadata` form parts and silently ignores unknown ones, so
# that one is open on BOTH faces and its closure round must add runtime
# validation, not just schema text (Codex #112 R3).
# Closing them is a frozen-contract edit (DR-002: version bump + owner), so
# this test is a RATCHET, not an allowlist: the pinned legacy set may only
# SHRINK (delete the entry in the same change that closes its schema); any
# NEW silently-open node fails the exact-equality assertion immediately.
_LEGACY_SILENT_OPEN_REQUEST_SCHEMAS = frozenset(
    {
        "BuildRequest",
        "IngestRequest",
        "ProjectCreate",
        "ProjectUpdate",
        "QueryRequest",
        "ReviewDecisionRequest",
        "SourceCreate",
        "POST /projects/{project}/uploads [multipart/form-data]",  # inline body
    }
)


def test_request_body_object_nodes_declare_additional_properties(spec: dict[str, Any]) -> None:
    """Why: PR #94 added additionalProperties:false to new bodies one review
    round at a time; the rule ("fixed semantics ⇒ closed") only holds forever
    if silence is mechanically impossible. This walks EVERY requestBody schema
    (all content types, refs resolved, nested properties/items/combinators)
    and demands each object node with fixed `properties` declares
    additionalProperties explicitly — enumerated from the artifact itself, per
    the #17 universal-test rule, so a new endpoint cannot dodge the sweep."""
    silent: set[str] = set()
    dynamic: set[str] = set()
    # EVERY schema-bearing JSON Schema 2020-12 keyword — enumerated as a table
    # so the sweep is grammar-driven, not example-driven (Codex #112 R3 named
    # prefixItems/dependentSchemas/if-then-else/patternProperties; listing only
    # the keywords a finding names is the class-9 anti-pattern)
    map_keywords = ("properties", "patternProperties", "dependentSchemas", "$defs")
    list_keywords = ("prefixItems", "oneOf", "anyOf", "allOf")
    single_keywords = (
        "items",
        "additionalProperties",
        "additionalItems",
        "unevaluatedItems",
        "unevaluatedProperties",
        "propertyNames",
        "contains",
        "contentSchema",
        "if",
        "then",
        "else",
        "not",
    )

    def walk(node: Any, label: str, seen: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        if "$dynamicRef" in node or "$dynamicAnchor" in node:
            # real dynamic-scope resolution is out of this lint's league —
            # fail LOUD instead of silently skipping, so $dynamicRef can never
            # become a bypass: using it in a request body requires extending
            # this walker first (Codex #112 R7)
            dynamic.add(label)
        if "$ref" in node:
            ref = node["$ref"]
            if ref not in seen:
                # the referenced schema gets its own RE-ROOTED walk so one pin
                # entry covers every endpoint referencing it — while NESTED
                # nodes extend the path, so a new silent node INSIDE a pinned
                # component keeps its own identity (Codex #112 R1). Resolve
                # the FULL pointer (a $defs-nested ref is legal — R3); a
                # dangling ref raises here, and that is correct fail-loud:
                # test_openapi_document_is_valid rejects it too.
                short = ref.removeprefix("#/components/schemas/").replace("/", ".")
                walk(_pointer(spec, ref), short, seen | {ref})
            # OpenAPI 3.1: a $ref may carry SIBLING schema keywords — judge
            # them under the ORIGINAL label instead of discarding them with
            # the ref (Codex #112 R2b); a bare {$ref} node ends here
            node = {k: v for k, v in node.items() if k != "$ref"}
            if not node:
                return
        # judgment: any OBJECT-SHAPING node must declare its openness. Shaping
        # markers cover the whole 2020-12 object vocabulary — `type: object`
        # (incl. the 3.1 nullable list form) plus every object-only applicator
        # (`{required: [name]}` alone is a valid, silently-open object
        # constraint — Codex #112 R5/R6). The declaration side accepts
        # `additionalProperties` OR `unevaluatedProperties`: the latter is an
        # explicit (composition-aware) closure statement, so flagging it
        # would be the class-9 over-block dual.
        node_type = node.get("type")
        object_typed = node_type == "object" or (
            isinstance(node_type, list) and "object" in node_type  # 3.1 nullable form
        )
        object_markers = (
            "properties",
            "patternProperties",
            "required",
            "dependentRequired",
            "dependentSchemas",
            "propertyNames",
            "minProperties",
            "maxProperties",
        )
        object_shaped = object_typed or any(k in node for k in object_markers)
        declared = "additionalProperties" in node or "unevaluatedProperties" in node
        if object_shaped and not declared:
            silent.add(label)
        for kw in map_keywords:
            for name, sub in (node.get(kw) or {}).items():
                sub_label = f"{label}.{name}" if kw == "properties" else f"{label}.{kw}[{name}]"
                walk(sub, sub_label, seen)
        for kw in list_keywords:
            for i, sub in enumerate(node.get(kw) or []):
                walk(sub, f"{label}.{kw}[{i}]", seen)
        for kw in single_keywords:
            sub = node.get(kw)
            if isinstance(sub, dict):
                walk(sub, f"{label}[]" if kw == "items" else f"{label}.{kw}", seen)

    def chase(node: Any) -> Any:
        # guarded $ref chain following: a cycle yields {} instead of a hang
        chain: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in chain:
                return {}
            chain.add(ref)
            node = _pointer(spec, ref)
        return node

    bodies = 0
    visited_refs: set[str] = set()
    # worklist of (path label, path-item map): top-level paths AND webhooks,
    # plus every operation's callbacks (whose path items nest recursively) —
    # a request body outside spec["paths"] must not dodge the sweep (#112 R7)
    pending = [(path, ops) for path, ops in spec["paths"].items()]
    pending += [(f"webhooks[{name}]", item) for name, item in spec.get("webhooks", {}).items()]
    while pending:
        path, ops = pending.pop()
        if not isinstance(ops, dict):
            continue  # x-* Specification Extensions in Paths/webhooks maps
        # a Path Item may itself be a $ref (3.1); overlap behavior between
        # referenced and inline fields is spec-undefined, so scan BOTH — the
        # inline fields now, the referenced item re-entering the worklist
        # (visited_refs bounds ref chains and cycles instead of hanging)
        if "$ref" in ops:
            ref = ops["$ref"]
            ops = {k: v for k, v in ops.items() if k != "$ref"}
            if ref not in visited_refs:
                visited_refs.add(ref)
                pending.append((path, _pointer(spec, ref)))
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            for cb_name, cb in op.get("callbacks", {}).items():
                if cb_name.startswith("x-") or not isinstance(cb, dict):
                    continue  # specification extensions are not Callback Objects
                if "$ref" in cb:
                    # persistent guard: a $ref'd callback expands once — a
                    # self-referential callback graph must drain, not hang
                    if cb["$ref"] in visited_refs:
                        continue
                    visited_refs.add(cb["$ref"])
                cb = chase(cb)  # Callback Object may be a (chained) $ref
                pending += [
                    (f"{path}.callbacks[{cb_name}][{expr}]", item)
                    for expr, item in cb.items()
                    # x-* extensions and scalar values are not Path Items
                    if not expr.startswith("x-") and isinstance(item, dict)
                ]
            if "requestBody" not in op:
                continue
            # a reusable components/requestBodies entry may itself be a
            # Reference Object, so follow the CHAIN of full pointers
            # (Codex #112 R2/R5); a dangling ref raises fail-loud and
            # test_openapi_document_is_valid rejects it independently
            rb = chase(op["requestBody"])
            for ctype, media in rb.get("content", {}).items():
                body_label = f"{method.upper()} {path} [{ctype}]"
                schema = media.get("schema")
                # an ABSENT, boolean-true, or empty schema accepts arbitrary
                # keys without declaring anything — that is a silently-open
                # body, not a skippable one (Codex #112 R8) — and the same
                # judgment applies through a $ref CHAIN to a true/{} component
                # (classify on the chased value, walk the original node so
                # component labels — and the pin — are preserved). `false`
                # rejects every body — explicit and closed, so it passes.
                resolved = chase(schema) if isinstance(schema, dict) else schema
                if schema is None or resolved is True or resolved == {}:
                    bodies += 1
                    silent.add(body_label)
                    continue
                if isinstance(schema, dict):
                    bodies += 1
                    walk(schema, body_label, frozenset())

    assert bodies >= 23  # the walk must actually cover the surface, not vacuously pass
    assert not dynamic, (
        f"$dynamicRef/$dynamicAnchor in request-body schema(s): {sorted(dynamic)} — this "
        "ratchet cannot resolve dynamic scopes; extend the walker before using them"
    )
    new_silent = silent - _LEGACY_SILENT_OPEN_REQUEST_SCHEMAS
    closed_legacy = _LEGACY_SILENT_OPEN_REQUEST_SCHEMAS - silent
    assert not new_silent, (
        f"new silently-open request-body object node(s): {sorted(new_silent)} — declare "
        "additionalProperties explicitly (false unless the node is a documented free-form bag)"
    )
    assert not closed_legacy, (
        f"legacy schemas now closed: {sorted(closed_legacy)} — shrink "
        "_LEGACY_SILENT_OPEN_REQUEST_SCHEMAS in the same change (the ratchet only tightens)"
    )
