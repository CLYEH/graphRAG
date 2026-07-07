"""Why: BA1b's HTTP-only logic must hold without a database — the opaque cursor
must round-trip and reject tampering (a mangled cursor paging from the top would
silently loop or skip), and the idempotency request-hash must be stable and
canonical (whitespace/key-order must not fake a conflict). The frozen served
OpenAPI staying put post-mount is covered by test_api_skeleton's served==frozen
test; the live CRUD/idempotency behavior is proved in the integration suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash
from api.pagination import decode_project_cursor, decode_source_cursor, encode_cursor

pytestmark = pytest.mark.contract


def test_project_cursor_round_trips() -> None:
    ts = datetime(2026, 7, 7, 12, 30, tzinfo=UTC)
    token = encode_cursor((ts, "proj-x"))
    assert decode_project_cursor(token) == (ts, "proj-x")


def test_source_cursor_round_trips() -> None:
    ts = datetime(2026, 7, 7, 12, 30, tzinfo=UTC)
    sid = uuid.uuid4()
    token = encode_cursor((ts, sid))
    assert decode_source_cursor(token) == (ts, sid)


@pytest.mark.parametrize("bad", ["not-base64!!", "", "YWJj", "eyJhIjogMX0="])
def test_malformed_cursor_is_a_validation_error(bad: str) -> None:
    """Bad base64, wrong arity, unparseable datetime → 400, never a silent
    reset to page one."""
    with pytest.raises(ApiError) as ei:
        decode_project_cursor(bad)
    assert ei.value.code is ErrorCode.VALIDATION_ERROR


def test_request_hash_is_stable_and_canonical() -> None:
    a = request_hash("POST", "/projects", b'{"name":"x","config":{}}')
    # same identity, only whitespace/key-order differs → same hash (no false conflict)
    b = request_hash("POST", "/projects", b'{ "config": {} , "name": "x" }')
    assert a == b
    # different body → different hash
    assert a != request_hash("POST", "/projects", b'{"name":"y"}')
    # different path (same key reused across endpoints) → different hash
    assert a != request_hash("POST", "/projects/p/sources", b'{"name":"x","config":{}}')


def test_translate_registry_error_maps_each_domain_error() -> None:
    from api.registry_errors import translate_registry_error
    from core.registry import ProjectExistsError, ProjectHasBuildsError, ProjectNotFoundError

    nf = translate_registry_error(ProjectNotFoundError("p"))
    assert nf.code is ErrorCode.PROJECT_NOT_FOUND and nf.details == {"project": "p"}
    # the flagged gap: exists/has-builds have no frozen conflict code → 400
    ex = translate_registry_error(ProjectExistsError("p"))
    assert ex.code is ErrorCode.VALIDATION_ERROR and ex.details == {"name": "p"}
    hb = translate_registry_error(ProjectHasBuildsError("p", 3))
    assert hb.code is ErrorCode.VALIDATION_ERROR and hb.details == {"project": "p", "builds": 3}
    # an unexpected error is re-raised, never silently reshaped into a 4xx
    with pytest.raises(RuntimeError):
        translate_registry_error(RuntimeError("boom"))


def test_dtos_project_the_contract_shape() -> None:
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from api.schemas import project_dto, source_dto
    from core.registry import Project, Source

    ts = _dt(2026, 1, 1, tzinfo=_UTC)
    p = Project(name="n", display_name="dn", description="d", config={"k": 1}, created_at=ts)
    assert project_dto(p) == {
        "name": "n",
        "display_name": "dn",
        "description": "d",
        "config": {"k": 1},
        "created_at": ts,
    }
    sid = uuid.uuid4()
    s = Source(id=sid, project="proj", kind="file", uri="u", metadata={"m": 1}, added_at=ts)
    dto = source_dto(s)
    assert dto == {"id": sid, "kind": "file", "uri": "u", "metadata": {"m": 1}, "added_at": ts}
    assert "project" not in dto  # contract Source is project-free (it's path context)


def test_config_null_rejected_but_omitted_is_fine() -> None:
    """config is `type: object` (non-nullable) in the contract — an explicit
    null is a 400 at the DTO, never a NOT NULL 500 in the registry. Omitting it
    stays unset (→ _UNSET → unchanged on PATCH); a dict passes."""
    import pydantic

    from api.schemas import ProjectCreate, ProjectUpdate

    with pytest.raises(pydantic.ValidationError):
        ProjectUpdate(config=None)  # explicit null → rejected
    with pytest.raises(pydantic.ValidationError):
        ProjectCreate(name="x", config=None)
    # omitted → not in the patch (so the registry leaves the column alone)
    assert "config" not in ProjectUpdate().model_dump(exclude_unset=True)
    assert ProjectUpdate(config={"a": 1}).model_dump(exclude_unset=True) == {"config": {"a": 1}}


def test_reject_unsupported_query() -> None:
    from starlette.requests import Request

    from api.routers._query import reject_unsupported_query

    def _req(qs: str) -> Request:
        return Request({"type": "http", "query_string": qs.encode(), "headers": []})

    reject_unsupported_query(_req("sort=created_at:desc"), "created_at")  # default → ok
    reject_unsupported_query(_req(""), "created_at")  # no sort → ok
    with pytest.raises(ApiError):  # non-default sort is rejected, not ignored
        reject_unsupported_query(_req("sort=name:asc"), "created_at")
    with pytest.raises(ApiError):  # any filter is rejected
        reject_unsupported_query(_req("filter%5Bx%5D=1"), "created_at")
