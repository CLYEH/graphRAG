"""Why: BA5's router owns the HTTP behaviors above core's decision function —
the §15 envelope with meta.build_id, the GAP error mappings (missing candidate
= true 404 + coarse code; §17 refusal = 400 with {status, decision} details),
the reason passthrough, the shared null-body guard, and the list's cursor
mechanics. These hold without Postgres (binding/decide/repo stubbed); the live
SQL/ledger behavior is the integration suites' job.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import db_conn
from api.pagination import decode_id_cursor
from core.resolve.decisions import InvalidReviewTransitionError, MergeCandidateNotFoundError

pytestmark = pytest.mark.contract

_TS = datetime(2026, 7, 10, tzinfo=UTC)
_BUILD = uuid.uuid4()


def _candidate(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project": "p",
        "build_id": _BUILD,
        "left_entity_id": uuid.uuid4(),
        "right_entity_id": uuid.uuid4(),
        "score": 0.85,
        "features": None,
        "status": "pending",
        "decision": None,
        "decided_by": None,
        "decided_at": None,
        "reason": None,
        "impact": None,
        "left_snapshot": None,
        "right_snapshot": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[db_conn] = _conn
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.review.{name}", fn)


def _bindable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name)

    async def fake_resolve(conn: Any, project: str) -> Any:
        return SimpleNamespace(project=project, build_id=_BUILD)

    _stub(monkeypatch, "get_project", fake_get_project)
    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)


class _FakeRepo:
    pages: Sequence[Any] = ()

    @classmethod
    def bound_to(cls, conn: Any, binding: Any) -> _FakeRepo:
        return cls()

    async def fetch_page(self, table: Any, *where: Any, order_by: Any, limit: int) -> Sequence[Any]:
        return type(self).pages


def test_candidate_dto_nullability_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (#55 rule): features is an optional NON-nullable object over a
    # nullable column → {}; decision/decided_*/reason/impact/snapshots are
    # contract-NULLABLE → null stays null, keys always present.
    _bindable(monkeypatch)
    _FakeRepo.pages = (_candidate(),)
    _stub(monkeypatch, "BuildScopedRepo", _FakeRepo)
    r = client.get("/projects/p/merge-candidates")
    (dto,) = r.json()["data"]
    assert dto["features"] == {}
    assert dto["impact"] is None and dto["left_snapshot"] is None
    assert dto["decision"] is None and dto["decided_at"] is None
    assert r.json()["meta"]["build_id"] == str(_BUILD)


def test_list_paginates_and_rejects_sort_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bindable(monkeypatch)
    rows = [_candidate() for _ in range(3)]
    _FakeRepo.pages = rows
    _stub(monkeypatch, "BuildScopedRepo", _FakeRepo)
    r = client.get("/projects/p/merge-candidates", params={"limit": 2})
    assert decode_id_cursor(r.json()["meta"]["next_cursor"]) == (rows[1].id,)
    assert (
        client.get("/projects/p/merge-candidates", params={"sort": "score:desc"}).status_code == 400
    )
    assert (
        client.get("/projects/p/merge-candidates", params={"filter[status]": "pending"}).status_code
        == 400
    )


def test_decide_maps_the_gap_errors_and_passes_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bindable(monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_decide(conn: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _candidate(
            status="approved",
            decision="approve",
            decided_by="console",
            decided_at=_TS,
            reason=kwargs["reason"],
        )

    _stub(monkeypatch, "decide_merge_candidate", fake_decide)
    cid = uuid.uuid4()
    r = client.post(f"/projects/p/merge-candidates/{cid}/approve", json={"reason": "dup"})
    assert r.status_code == 200
    assert r.json()["data"]["reason"] == "dup"
    assert captured["verb"] == "approve" and captured["decided_by"] == "console"
    assert captured["reason"] == "dup" and captured["candidate_id"] == cid

    async def missing(conn: Any, **kwargs: Any) -> Any:
        raise MergeCandidateNotFoundError("p", kwargs["candidate_id"])

    _stub(monkeypatch, "decide_merge_candidate", missing)
    r = client.post(f"/projects/p/merge-candidates/{cid}/reject")
    assert r.status_code == 404  # GAP: true status, coarse code
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    async def refused(conn: Any, **kwargs: Any) -> Any:
        raise InvalidReviewTransitionError(kwargs["candidate_id"], "approved", kwargs["verb"])

    _stub(monkeypatch, "decide_merge_candidate", refused)
    r = client.post(f"/projects/p/merge-candidates/{cid}/defer")
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"status": "approved", "decision": "defer"}


def test_decide_rejects_null_body_and_unknown_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bindable(monkeypatch)

    async def fail_decide(conn: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not run")

    _stub(monkeypatch, "decide_merge_candidate", fail_decide)
    cid = uuid.uuid4()
    r = client.post(
        f"/projects/p/merge-candidates/{cid}/approve",
        content=b" null ",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400  # the #53 R5 class, via the shared guard
    r = client.post(f"/projects/p/merge-candidates/{cid}/approve", json={"verdict": "yes"})
    assert r.status_code == 400  # extra=forbid
