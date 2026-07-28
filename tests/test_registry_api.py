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
from api.pagination import decode_sorted_cursor, encode_sorted_cursor, scope_fingerprint

pytestmark = pytest.mark.contract


def test_project_cursor_round_trips() -> None:
    """Projects page by (created_at desc, name desc) — created_at alone is not
    unique, so the PK rides along as the tie-break (BA1b).

    QA8/D6: this used to round-trip through a bare ``decode_project_cursor``,
    and that was the defect — an UNTAGGED (datetime, str) pair is decodable by
    any listing with the same arity. It was measured: a /sources token decoded
    cleanly here, so one project's source listing re-anchored the global
    project list. The tag is now part of the round trip, not an add-on to it.
    """
    ts = datetime(2026, 7, 7, 12, 30, tzinfo=UTC)
    tag = f"created_at:desc|{scope_fingerprint('projects', '', None, {})}"
    token = encode_sorted_cursor(tag, (ts, "proj-x"))
    assert decode_sorted_cursor(token, tag, (datetime, str)) == (ts, "proj-x")

    # the measured interchange, now refused from the other direction too
    sources_tag = f"added_at:desc|{scope_fingerprint('sources', 'p', None, {})}"
    with pytest.raises(ApiError) as ei:
        decode_sorted_cursor(
            encode_sorted_cursor(sources_tag, (ts, uuid.uuid4())), tag, (datetime, str)
        )
    assert ei.value.code is ErrorCode.VALIDATION_ERROR


def test_source_cursor_binds_its_project() -> None:
    """QA8/D6: a sources cursor carried no project, so replaying it under
    another project re-anchored THAT project's list — a cross-tenant read, not
    a miscounted page. The project is in the tag, so the two are distinct
    tokens even for identical (added_at, id) values.
    """
    ts = datetime(2026, 7, 7, 12, 30, tzinfo=UTC)
    sid = uuid.uuid4()
    tag = f"added_at:desc|{scope_fingerprint('sources', 'p', None, {})}"
    token = encode_sorted_cursor(tag, (ts, sid))
    assert decode_sorted_cursor(token, tag, (datetime, uuid.UUID)) == (ts, sid)

    other = f"added_at:desc|{scope_fingerprint('sources', 'q', None, {})}"
    with pytest.raises(ApiError) as ei:
        decode_sorted_cursor(token, other, (datetime, uuid.UUID))
    assert ei.value.code is ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("bad", ["not-base64!!", "", "YWJj", "eyJhIjogMX0="])
def test_malformed_cursor_is_a_validation_error(bad: str) -> None:
    """Bad base64, wrong arity, unparseable datetime → 400, never a silent
    reset to page one."""
    with pytest.raises(ApiError) as ei:
        decode_sorted_cursor(bad, "created_at:desc|whatever", (datetime, str))
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
    from core.registry import (
        ProjectExistsError,
        ProjectHasActiveJobsError,
        ProjectHasBuildsError,
        ProjectNotFoundError,
    )

    nf = translate_registry_error(ProjectNotFoundError("p"))
    assert nf.code is ErrorCode.PROJECT_NOT_FOUND and nf.details == {"project": "p"}
    # the flagged gap: exists/has-builds/has-active-jobs have no frozen conflict
    # code → all 400 (a delete against the new active-jobs guard must NOT fall
    # through to a 500)
    ex = translate_registry_error(ProjectExistsError("p"))
    assert ex.code is ErrorCode.VALIDATION_ERROR and ex.details == {"name": "p"}
    hb = translate_registry_error(ProjectHasBuildsError("p", 3))
    assert hb.code is ErrorCode.VALIDATION_ERROR and hb.details == {"project": "p", "builds": 3}
    aj = translate_registry_error(ProjectHasActiveJobsError("p", 2))
    assert aj.code is ErrorCode.VALIDATION_ERROR and aj.details == {"project": "p", "jobs": 2}
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
    assert dto == {
        "id": sid,
        "kind": "file",
        "uri": "u",
        "enabled": True,  # SRC2 (DR-013): the soft-disable flag is always emitted
        "metadata": {"m": 1},
        "added_at": ts,
    }
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

    # GOV4: an endpoint that implements a facet names it — everything else
    # still fails loud (a 200 that pretends a filter took effect misleads)
    allowed = frozenset({"status"})
    reject_unsupported_query(_req("filter%5Bstatus%5D=approved"), "id", allowed)  # facet → ok
    with pytest.raises(ApiError):  # field outside the allowlist
        reject_unsupported_query(_req("filter%5Bother%5D=1"), "id", allowed)
    with pytest.raises(ApiError):  # bare non-deepObject spelling used to slip
        # past the "filter[" prefix check and silently no-op (GAPS O4)
        reject_unsupported_query(_req("filter=status:approved"), "id", allowed)
    with pytest.raises(ApiError):  # malformed bracket never half-matches
        reject_unsupported_query(_req("filter%5Bstatus=approved"), "id", allowed)
    # unrelated params that merely share the prefix are NOT filters
    reject_unsupported_query(_req("filtering=1"), "id", allowed)
