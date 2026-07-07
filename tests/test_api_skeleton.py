"""Why: BA0 is the seam every Track-2 router mounts on, so its cross-cutting
guarantees must hold before any endpoint exists — the PUBLISHED schema is the
frozen contract (DR-002, not a drifting code-gen), every error is the §15
envelope with a frozen code, the request_id threads through, and the auth
placeholder keeps the scheme while deferring policy. The error vocabulary is
pinned in lockstep with the contract so a code added on one side without the
other fails here, not in production."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any

import pytest
import yaml
from fastapi import Depends, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator

from api import create_app
from api.app import _frozen_contract
from api.auth import Principal, current_principal
from api.envelope import error_body, success
from api.errors import ApiError, ErrorCode, http_status_for

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "contracts" / "openapi.yaml"

pytestmark = pytest.mark.contract


@pytest.fixture()
def client() -> TestClient:
    """A test app with a few throwaway routes that exercise the skeleton —
    BA0 ships no domain routes, so the machinery is proven via local ones
    mounted only for this test (raise_server_exceptions off so the uncaught
    handler runs instead of the TestClient re-raising)."""
    app = create_app()

    class _Body(BaseModel):
        name: str

    class _StrictBody(BaseModel):
        # a custom validator that raises — its ctx carries the original
        # ValueError, a non-JSON-serializable object (the round-1 crash path)
        name: str

        @field_validator("name")
        @classmethod
        def _reject(cls, value: str) -> str:
            raise ValueError("name is never valid")

    @app.get("/_t/ok")
    async def _ok(request: Request) -> dict[str, Any]:
        return success(
            {"hello": "world"},
            request_id=request.state.request_id,
            elapsed_ms=0,
        )

    @app.get("/_t/boom")
    async def _boom() -> None:
        raise ApiError(ErrorCode.PROJECT_NOT_FOUND, "no such project", details={"project": "x"})

    @app.get("/_t/crash")
    async def _crash() -> None:
        raise RuntimeError("unexpected — must not leak")

    @app.get("/_t/unavailable")
    async def _unavailable() -> None:
        # a raised 5xx HTTPException — the envelope's INTERNAL branch
        raise HTTPException(status_code=503, detail="downstream down")

    @app.post("/_t/validate")
    async def _validate(body: _Body) -> dict[str, Any]:
        return success(body.model_dump(), request_id=uuid.uuid4(), elapsed_ms=0)

    @app.post("/_t/strict")
    async def _strict(body: _StrictBody) -> dict[str, Any]:
        return success(body.model_dump(), request_id=uuid.uuid4(), elapsed_ms=0)

    @app.get("/_t/whoami")
    async def _whoami(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> dict[str, Any]:
        return success(
            {"token": principal.token, "auth": principal.is_authenticated},
            request_id=uuid.uuid4(),
            elapsed_ms=0,
        )

    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------- pure units


def test_error_code_enum_is_lockstep_with_the_contract() -> None:
    """DR-002: the API's ErrorCode must equal the frozen contract enum — a
    code added on either side alone fails CI, never drifts to production."""
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    frozen = set(contract["components"]["schemas"]["ErrorCode"]["enum"])
    assert {code.value for code in ErrorCode} == frozen


def _contract_status_mapping() -> dict[str, int]:
    """Parse the frozen contract's documented status→code prose (the
    ``responses.Error`` description) into ``{CODE: status}`` — the source of
    truth this module's ``_HTTP_STATUS`` must match (DR-002)."""
    import re

    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    prose = contract["components"]["responses"]["Error"]["description"]
    mapping: dict[str, int] = {}
    # e.g. "400 `VALIDATION_ERROR`/`QUERY_UNSAFE` · 404 `PROJECT_NOT_FOUND`/…"
    for status, codes in re.findall(r"(\d{3})\s+(`\w+`(?:/`\w+`)*)", prose):
        for code in re.findall(r"`(\w+)`", codes):
            mapping[code] = int(status)
    return mapping


def test_http_status_map_matches_the_contract_prose() -> None:
    """DR-002: the code→status map must equal what the frozen contract
    documents — not just be in-range. A divergence (e.g. VALIDATION_ERROR as
    422 vs the contract's 400) fails here instead of shipping."""
    expected = _contract_status_mapping()
    assert {code.value: http_status_for(code) for code in ErrorCode} == expected
    # and the contract documents every code (no code without a mapping)
    assert set(expected) == {code.value for code in ErrorCode}


def test_success_envelope_shape() -> None:
    rid, bid = uuid.uuid4(), uuid.uuid4()
    env = success({"x": 1}, request_id=rid, elapsed_ms=7, build_id=bid)
    assert env == {
        "data": {"x": 1},
        "meta": {"request_id": str(rid), "build_id": str(bid), "elapsed_ms": 7},
    }
    # paginated adds next_cursor (null on the last page); build_id null when absent
    page = success([], request_id=rid, elapsed_ms=1, paginated=True, next_cursor=None)
    assert page["meta"]["next_cursor"] is None and page["meta"]["build_id"] is None


def test_error_envelope_details_is_null_not_absent() -> None:
    rid = uuid.uuid4()
    body = error_body(ErrorCode.INTERNAL, "boom", request_id=rid, details=None)
    assert body["error"] == {
        "code": "INTERNAL",
        "message": "boom",
        "details": None,  # present, not omitted (frozen Error shape)
        "request_id": str(rid),
    }


# ---------------------------------------------------------- app integration


def test_openapi_served_is_the_frozen_contract(client: TestClient) -> None:
    """The published schema IS contracts/openapi.yaml (DR-002) — not a
    FastAPI code-generated one that could drift."""
    served = client.get("/openapi.json").json()
    assert served == _frozen_contract()
    assert "bearerAuth" in served["components"]["securitySchemes"]


def test_success_response_carries_request_id_meta_and_header(client: TestClient) -> None:
    resp = client.get("/_t/ok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == {"hello": "world"}
    rid = body["meta"]["request_id"]
    uuid.UUID(rid)  # a real uuid
    assert resp.headers["X-Request-ID"] == rid  # header echoes the meta id


def test_api_error_renders_the_frozen_envelope_with_mapped_status(
    client: TestClient,
) -> None:
    resp = client.get("/_t/boom")
    assert resp.status_code == 404  # PROJECT_NOT_FOUND → 404
    err = resp.json()["error"]
    assert err["code"] == "PROJECT_NOT_FOUND"
    assert err["details"] == {"project": "x"}
    uuid.UUID(err["request_id"])
    # error responses carry X-Request-ID too (they bypass the middleware)
    assert resp.headers["X-Request-ID"] == err["request_id"]


def test_uncaught_exception_becomes_internal_without_leaking(client: TestClient) -> None:
    resp = client.get("/_t/crash")
    assert resp.status_code == 500
    err = resp.json()["error"]
    assert err["code"] == "INTERNAL"
    assert err["message"] == "internal error"  # no stack, no original message
    assert "unexpected" not in resp.text


def test_body_validation_becomes_validation_error(client: TestClient) -> None:
    resp = client.post("/_t/validate", json={"wrong": "field"})
    assert resp.status_code == 400  # the contract maps VALIDATION_ERROR → 400
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"] and "errors" in err["details"]


def test_custom_validator_error_serializes_not_crashes(client: TestClient) -> None:
    """A custom validator's error carries the original ValueError in ctx (a
    non-JSON object); the handler must jsonable_encode it and still return
    400 — not crash serialization into the uncaught-500 path (Codex round 1)."""
    resp = client.post("/_t/strict", json={"name": "anything"})
    assert resp.status_code == 400  # NOT 500 — the ctx object was encoded
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"] and "errors" in err["details"]


def test_unknown_route_wears_the_frozen_envelope_not_detail(client: TestClient) -> None:
    """A framework 404 (unknown path) must be the frozen envelope, never
    Starlette's {"detail": ...} (Codex round 1: the BA0 no-default-shape
    guarantee covers HTTPException too)."""
    resp = client.get("/_t/does-not-exist")
    assert resp.status_code == 404  # the true HTTP status is preserved
    body = resp.json()
    assert "detail" not in body  # no leaked framework shape
    assert body["error"]["code"] == "VALIDATION_ERROR"  # 4xx → client didn't conform
    assert resp.headers["X-Request-ID"] == body["error"]["request_id"]


def test_raised_5xx_http_exception_maps_to_internal(client: TestClient) -> None:
    """A 5xx HTTPException wears the envelope with INTERNAL, true status
    preserved (the server-fault branch of the framework handler)."""
    resp = client.get("/_t/unavailable")
    assert resp.status_code == 503  # true status kept
    body = resp.json()
    assert "detail" not in body
    assert body["error"]["code"] == "INTERNAL"  # 5xx → server fault


def test_auth_placeholder_extracts_token_and_admits_anonymous(client: TestClient) -> None:
    """§23: keep the scheme, defer the policy — a token is extracted, a
    missing one is admitted anonymous (the frozen enum has no auth code, so
    the placeholder never rejects)."""
    with_token = client.get("/_t/whoami", headers={"Authorization": "Bearer abc123"})
    assert with_token.json()["data"] == {"token": "abc123", "auth": False}
    anonymous = client.get("/_t/whoami")
    assert anonymous.status_code == 200
    assert anonymous.json()["data"] == {"token": None, "auth": False}
