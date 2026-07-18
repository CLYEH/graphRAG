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
from core.graph.proposals import (
    InvalidProposalTransitionError,
    OntologyConfigIncompleteError,
    OntologyProposalNotFoundError,
)
from core.registry import ProjectNotFoundError
from core.resolve.decisions import (
    EntityNotFoundError,
    InvalidReviewTransitionError,
    MergeCandidateNotFoundError,
    RelationNotFoundError,
)

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
    # GOV4: filter[status] is THIS list's implemented facet (any §17 value) —
    # everything else still fails loud, never a 200 that pretends (GAPS O4)
    assert (
        client.get("/projects/p/merge-candidates", params={"filter[status]": "pending"}).status_code
        == 200
    )
    assert (
        client.get("/projects/p/merge-candidates", params={"filter[status]": "bogus"}).status_code
        == 400
    )
    assert (
        client.get("/projects/p/merge-candidates", params={"filter[score]": "1"}).status_code == 400
    )
    assert (
        client.get("/projects/p/merge-candidates", params={"filter": "status:pending"}).status_code
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
        raise InvalidReviewTransitionError(
            "merge candidate", kwargs["candidate_id"], "approved", kwargs["verb"]
        )

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


# --- GOV3: ontology proposal pool ---------------------------------------------


def _proposal(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project": "p",
        "kind": "entity",
        "type_name": "Exhibit",
        "proposal_key": "fpv1:entity|exhibit",
        "fingerprint_version": 1,
        "example": "區域探索廳",
        "chunk_ref": "doc-h:0",
        "status": "proposed",
        "decided_by": None,
        "decided_at": None,
        "reason": None,
        "created_at": _TS,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _present_project(monkeypatch: pytest.MonkeyPatch) -> None:
    async def present(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name)

    _stub(monkeypatch, "get_project", present)


def test_list_proposals_shape_pagination_and_no_build_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the pool is NOT build-scoped — the list carries no meta.build_id (unlike
    # merge-candidates), pages by id desc, and emits the full contract shape
    # with nullable decision fields as null.
    _present_project(monkeypatch)
    rows = [_proposal(), _proposal()]

    async def fake_list(conn: Any, project: str, *, limit: int, after: Any, status: Any) -> Any:
        return rows, rows[-1].id  # a next page exists

    _stub(monkeypatch, "list_ontology_proposals", fake_list)
    r = client.get("/projects/p/ontology-proposals")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["build_id"] is None  # pool is not build-scoped
    assert decode_id_cursor(body["meta"]["next_cursor"]) == (rows[-1].id,)
    dto = body["data"][0]
    assert dto["kind"] == "entity" and dto["status"] == "proposed"
    assert dto["decided_by"] is None and dto["decided_at"] is None  # nullable, present


def test_list_proposals_404_when_project_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "get_project", missing)
    r = client.get("/projects/x/ontology-proposals")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_list_proposals_status_filter_passes_the_vocabulary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # filter[status] reads the §17 ontology-proposal machine, so a legal status
    # reaches the query and an out-of-vocabulary one is rejected (400) before it.
    _present_project(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake_list(conn: Any, project: str, *, limit: int, after: Any, status: Any) -> Any:
        seen["status"] = status
        return [], None

    _stub(monkeypatch, "list_ontology_proposals", fake_list)
    assert (
        client.get(
            "/projects/p/ontology-proposals", params={"filter[status]": "accepted"}
        ).status_code
        == 200
    )
    assert seen["status"] == "accepted"
    # an unknown status is not in the machine → rejected loud, never queried
    r = client.get("/projects/p/ontology-proposals", params={"filter[status]": "bogus"})
    assert r.status_code == 400


def test_accept_proposal_200_and_echoes_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_decide(
        conn: Any, *, project: str, proposal_id: Any, verb: str, **kw: Any
    ) -> Any:
        seen["verb"] = verb
        return _proposal(status="accepted", decided_by="console", decided_at=_TS)

    _stub(monkeypatch, "decide_ontology_proposal", fake_decide)
    pid = uuid.uuid4()
    r = client.post(f"/projects/p/ontology-proposals/{pid}/accept")
    assert r.status_code == 200
    assert seen["verb"] == "accept"  # the endpoint's verb reached the decision
    assert r.json()["data"]["status"] == "accepted"


def test_reject_proposal_maps_verb_and_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PROPOSAL_NOT_FOUND (the new v1.3 code) and the §17 refusal (400) map
    # through the shared translation point / GAP mapping.
    pid = uuid.uuid4()

    async def not_found(conn: Any, *, project: str, proposal_id: Any, **kw: Any) -> Any:
        raise OntologyProposalNotFoundError(project, proposal_id)

    _stub(monkeypatch, "decide_ontology_proposal", not_found)
    r = client.post(f"/projects/p/ontology-proposals/{pid}/reject")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROPOSAL_NOT_FOUND"

    async def missing_project(conn: Any, *, project: str, proposal_id: Any, **kw: Any) -> Any:
        raise ProjectNotFoundError(project)

    _stub(monkeypatch, "decide_ontology_proposal", missing_project)
    r = client.post(f"/projects/p/ontology-proposals/{pid}/reject")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    async def refused(conn: Any, *, project: str, proposal_id: Any, verb: str, **kw: Any) -> Any:
        raise InvalidProposalTransitionError(proposal_id, "accepted", verb)

    _stub(monkeypatch, "decide_ontology_proposal", refused)
    r = client.post(f"/projects/p/ontology-proposals/{pid}/reject")
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"status": "accepted", "decision": "reject"}


def test_accept_refused_on_incomplete_ontology_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex #97 R1: accept into a project whose ontology config can't absorb the
    # type is a 400 (the curator fixes the ontology, then re-accepts) — never a
    # 200 that silently bricks the next build.
    pid = uuid.uuid4()

    async def incomplete(conn: Any, *, project: str, proposal_id: Any, **kw: Any) -> Any:
        raise OntologyConfigIncompleteError(project, "ontology missing")

    _stub(monkeypatch, "decide_ontology_proposal", incomplete)
    r = client.post(f"/projects/p/ontology-proposals/{pid}/accept")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["details"]["project"] == "p"


def test_proposal_decide_rejects_null_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(conn: Any, **kw: Any) -> Any:
        raise AssertionError("must not run on a null body")

    _stub(monkeypatch, "decide_ontology_proposal", fail)
    pid = uuid.uuid4()
    r = client.post(
        f"/projects/p/ontology-proposals/{pid}/accept",
        content=b" null ",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400  # the #53 R5 shared guard


# --- GOV2: entity / relation review -------------------------------------------


def _entity_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project": "p",
        "build_id": _BUILD,
        "type": "Person",
        "canonical_name": "Alice",
        "entity_key": "fpv1:person|alice",
        "attributes": None,
        "status": "active",
        "review_status": "approved",
        "created_by": "llm",
        "created_at": _TS,
        "updated_at": _TS,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _relation_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project": "p",
        "build_id": _BUILD,
        "src_entity_id": uuid.uuid4(),
        "dst_entity_id": uuid.uuid4(),
        "type": "WORKS_AT",
        "attributes": None,
        "relation_signature": "fpv1:alice|works_at|acme",
        "status": "rejected",
        "review_status": "rejected",
        "created_by": "llm",
        "confidence": 0.9,
        "created_at": _TS,
        "updated_at": _TS,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_approve_entity_200_stamps_build_id_and_echoes_review_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GOV2: entity approve returns the active build's updated row (review_status
    # approved), stamps meta.build_id (build-scoped, unlike the proposal pool),
    # and hands the verb to the core decide.
    _bindable(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake_decide(conn: Any, *, verb: str, build_id: Any, **kw: Any) -> Any:
        seen["verb"] = verb
        seen["build_id"] = build_id
        seen["entity_id"] = kw["entity_id"]
        return _entity_row(review_status="approved")

    _stub(monkeypatch, "decide_entity", fake_decide)
    eid = uuid.uuid4()
    r = client.post(f"/projects/p/entities/{eid}/approve")
    assert r.status_code == 200
    assert seen["verb"] == "approve" and seen["entity_id"] == eid
    assert seen["build_id"] == _BUILD  # the active binding reached the decide
    body = r.json()
    assert body["data"]["review_status"] == "approved"
    assert body["meta"]["build_id"] == str(_BUILD)


def test_entity_decide_maps_not_found_and_transition_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bindable(monkeypatch)
    eid = uuid.uuid4()

    async def missing(conn: Any, **kw: Any) -> Any:
        raise EntityNotFoundError("p", kw["entity_id"])

    _stub(monkeypatch, "decide_entity", missing)
    r = client.post(f"/projects/p/entities/{eid}/reject")
    assert r.status_code == 404  # GAP: true status, coarse code

    async def refused(conn: Any, *, verb: str, **kw: Any) -> Any:
        raise InvalidReviewTransitionError("entity", kw["entity_id"], "approved", verb)

    _stub(monkeypatch, "decide_entity", refused)
    r = client.post(f"/projects/p/entities/{eid}/approve")
    assert r.status_code == 400
    assert r.json()["error"]["details"] == {"status": "approved", "decision": "approve"}


def test_reject_relation_200_and_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bindable(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake_decide(conn: Any, *, verb: str, **kw: Any) -> Any:
        seen["verb"] = verb
        seen["relation_id"] = kw["relation_id"]
        return _relation_row(status="rejected", review_status="rejected")

    _stub(monkeypatch, "decide_relation", fake_decide)
    rid = uuid.uuid4()
    r = client.post(f"/projects/p/relations/{rid}/reject")
    assert r.status_code == 200
    assert seen["verb"] == "reject" and seen["relation_id"] == rid
    data = r.json()["data"]
    assert data["status"] == "rejected" and data["review_status"] == "rejected"
    assert "evidence" not in data  # decide response is list-shaped, no evidence

    async def missing(conn: Any, **kw: Any) -> Any:
        raise RelationNotFoundError("p", kw["relation_id"])

    _stub(monkeypatch, "decide_relation", missing)
    r = client.post(f"/projects/p/relations/{rid}/approve")
    assert r.status_code == 404


def test_entity_relation_decide_rejects_null_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bindable(monkeypatch)

    async def fail(conn: Any, **kw: Any) -> Any:
        raise AssertionError("must not run on a null body")

    _stub(monkeypatch, "decide_entity", fail)
    _stub(monkeypatch, "decide_relation", fail)
    for path in (
        f"/projects/p/entities/{uuid.uuid4()}/approve",
        f"/projects/p/relations/{uuid.uuid4()}/reject",
    ):
        r = client.post(path, content=b" null ", headers={"Content-Type": "application/json"})
        assert r.status_code == 400, path  # the #53 R5 shared guard
