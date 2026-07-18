"""Why: the eval endpoint's whole job is to run the project's golden set against
a NAMED build as an async job and land the score where the activation gate reads
it — "zero new coupling" (DR-010). So the behaviors that matter are the job
contract: a 202 + job envelope on acceptance, the single-active-job 409
(overlapping work must not double-run an eval), the project 404, and — the
correlation that makes the whole feature work — that the build named in the PATH
is exactly what gets enqueued for the worker to evaluate.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import arq_redis_provider, db_conn
from core.config import get_settings
from core.eval.idempotency import eval_inputs_fingerprint
from core.registry import JobConflictError, ProjectNotFoundError

pytestmark = pytest.mark.contract

_BUILD = uuid.uuid4()
_URL = f"/projects/demo/builds/{_BUILD}/eval"


@pytest.fixture(autouse=True)
def _stub_registry_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """CFG1: the accept-time fingerprint reads the registry for its policy
    component — these hermetic tests run on fake conns, so the read is
    stubbed to 'no project row' (empty policy bytes; the fingerprint only
    needs stability here, and the job path already fakes its own errors)."""

    async def none_project(conn: object, name: str, *, for_update: bool = False) -> None:
        return None

    monkeypatch.setattr("api.routers.builds.get_project", none_project)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()

    def _fake_queue() -> Callable[[], Awaitable[object]]:
        async def _get() -> object:
            return object()

        return _get

    app.dependency_overrides[db_conn] = _conn
    app.dependency_overrides[arq_redis_provider] = _fake_queue
    with TestClient(app) as c:
        yield c


def _created_job(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub create_job_exclusive to return a queued job, capturing its kwargs; also stub
    the eval-inputs-fingerprint pin (a no-op on the fake conn), capturing the job id it
    pinned and the fingerprint value (UXC1b triage 27)."""
    captured: dict[str, Any] = {}
    job_id = uuid.uuid4()
    captured["job_id_created"] = job_id

    async def fake_create(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        captured["kind"] = kind
        captured["build_id"] = build_id
        return SimpleNamespace(id=job_id, status="queued")

    async def fake_pin(conn: Any, jid: Any, fingerprint: str) -> None:
        captured["pinned_job_id"] = jid
        captured["pinned_fingerprint"] = fingerprint

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", fake_create)
    monkeypatch.setattr("api.routers.builds.set_eval_inputs_fingerprint", fake_pin)
    return captured


def _capture_enqueue(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_enqueue(redis: Any, project: str, job_id: Any, build_id: Any) -> bool:
        captured["project"] = project
        captured["job_id"] = job_id
        captured["build_id"] = build_id
        return True

    monkeypatch.setattr("api.routers.builds.enqueue_eval", fake_enqueue)
    return captured


def test_eval_returns_202_and_job_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _created_job(monkeypatch)
    _capture_enqueue(monkeypatch)
    resp = client.post(_URL)
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "queued" and "job_id" in data


def test_eval_records_kind_and_target_build(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _created_job(monkeypatch)
    enqueued = _capture_enqueue(monkeypatch)
    resp = client.post(_URL)
    assert resp.status_code == 202
    # the job is an 'eval' job bound to the build named in the path...
    assert created["kind"] == "eval"
    assert created["build_id"] == _BUILD
    # ...and THAT build is what the worker is handed (the correlation that makes
    # the score land on the right build's eval column)
    assert enqueued["build_id"] == _BUILD


def test_eval_pins_the_accept_time_inputs_fingerprint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (triage 27): the endpoint pins the ACCEPT-time golden+policy fingerprint on the
    # created job so the worker can fail loud if the inputs drift before dispatch (else it
    # would score bytes the client never accepted, under the accepted idempotency key). Pin
    # that the pin targets the created job AND carries exactly the accept-time fingerprint
    # (the same value folded into the idempotency hash).
    created = _created_job(monkeypatch)
    _capture_enqueue(monkeypatch)
    resp = client.post(_URL)
    assert resp.status_code == 202
    assert created["pinned_job_id"] == created["job_id_created"]
    # the stubbed registry has no project row → empty policy bytes (CFG1:
    # the policy component of the fingerprint is the registry block)
    expected = eval_inputs_fingerprint(Path(get_settings().projects_dir), "demo", b"")
    assert created["pinned_fingerprint"] == expected


def test_eval_overlapping_job_is_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def conflict(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        raise JobConflictError(project, uuid.uuid4())

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", conflict)
    resp = client.post(_URL)
    assert resp.status_code == 409


def test_eval_unknown_project_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(conn: Any, project: str, kind: str, *, build_id: Any = None) -> Any:
        raise ProjectNotFoundError(project)

    monkeypatch.setattr("api.routers.builds.create_job_exclusive", missing)
    resp = client.post(_URL)
    assert resp.status_code == 404


def test_eval_folds_the_request_body_into_the_idempotency_hash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: the endpoint is bodyless, but FastAPI still accepts a body and the sibling
    # bodyless endpoint (rollback) folds `await request.body()` into its idempotency
    # hash. If eval hashed only the eval-input fingerprint, a reused Idempotency-Key
    # with a DIFFERENT body would replay the stored job instead of raising the §27
    # IDEMPOTENCY_CONFLICT, and the stray body would be silently ignored. So the actual
    # request body MUST change the request hash (with the fingerprint held constant
    # here, the body is the only thing varying — a revert-probe on the old fingerprint-
    # only hash, under which all three hashes would collide).
    monkeypatch.setattr("api.routers.builds.eval_inputs_fingerprint", lambda *a: "FP")
    seen: list[str] = []

    async def fake_run_idempotent(
        conn: Any, *, req_hash: str, **kw: Any
    ) -> tuple[int, dict[str, Any]]:
        seen.append(req_hash)
        return 202, {"data": {"status": "queued", "job_id": str(uuid.uuid4())}}

    monkeypatch.setattr("api.routers.builds.run_idempotent", fake_run_idempotent)
    headers = {"Idempotency-Key": "k1"}
    client.post(_URL, headers=headers, content=b"one")
    client.post(_URL, headers=headers, content=b"two")
    client.post(_URL, headers=headers, content=b"one")
    assert seen[0] != seen[1]  # a different body ⇒ a different hash (no stale replay)
    assert seen[0] == seen[2]  # a genuine duplicate (same body) still hashes the same


def test_pin_is_recomputed_under_the_job_lock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#93 R2: a PATCH committing between the unlocked request-hash read and
    create_job_exclusive's projects-row lock must not poison the pin — the
    stored pin derives from the LOCKED re-read, so the accepted job is
    internally consistent with what the worker's dispatch read will see
    (the unlocked read only scopes the idempotency request hash)."""
    from core.eval.idempotency import policy_fingerprint_bytes

    created = _created_job(monkeypatch)
    _capture_enqueue(monkeypatch)

    calls = {"n": 0}
    old_cfg = {"query_policy": {"max_top_k": 1}}
    new_cfg = {"query_policy": {"max_top_k": 2}}

    async def racing_project(conn: Any, name: str, **kw: Any) -> Any:
        # first read (request hash, unlocked) sees the OLD policy; every read
        # after the lock sees the NEW one — the simulated racing PATCH
        calls["n"] += 1
        return SimpleNamespace(config=old_cfg if calls["n"] == 1 else new_cfg)

    monkeypatch.setattr("api.routers.builds.get_project", racing_project)
    resp = client.post(_URL)
    assert resp.status_code == 202
    expected_locked = eval_inputs_fingerprint(
        Path(get_settings().projects_dir), "demo", policy_fingerprint_bytes(new_cfg)
    )
    stale = eval_inputs_fingerprint(
        Path(get_settings().projects_dir), "demo", policy_fingerprint_bytes(old_cfg)
    )
    assert created["pinned_fingerprint"] == expected_locked
    assert created["pinned_fingerprint"] != stale  # the discriminating half


def test_the_kept_idempotency_identity_is_the_locked_pin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#93 R7: a PATCH committing between the unlocked request-hash read and
    the projects-row lock must not leave the §27 record claiming an identity
    the job wasn't scored under — the record's KEPT hash (run_idempotent's
    rekey) must be derived from the LOCKED fingerprint, the exact value pinned
    on the job. Then a retry under the accepted policy REPLAYS, and a policy
    flipped BACK conflicts — never a silent replay of a job scored against
    different inputs. Revert-probe: drop the rekey (keep the record on the
    unlocked hash) and the kept identity stays derived from the stale OLD
    config, failing the second assertion."""
    from fastapi.encoders import jsonable_encoder

    from api.idempotency import request_hash
    from core.eval.idempotency import policy_fingerprint_bytes

    created = _created_job(monkeypatch)
    _capture_enqueue(monkeypatch)

    calls = {"n": 0}
    old_cfg = {"query_policy": {"max_top_k": 1}}
    new_cfg = {"query_policy": {"max_top_k": 2}}

    async def racing_project(conn: Any, name: str, **kw: Any) -> Any:
        # the unlocked read sees the OLD policy; the under-lock read sees the
        # NEW one — the simulated PATCH landing in between
        calls["n"] += 1
        return SimpleNamespace(config=old_cfg if calls["n"] == 1 else new_cfg)

    monkeypatch.setattr("api.routers.builds.get_project", racing_project)

    captured: dict[str, Any] = {}

    async def fake_run_idempotent(
        conn: Any, *, req_hash: str, produce: Any, rekey: Any = None, **kw: Any
    ) -> tuple[int, dict[str, Any]]:
        captured["initial"] = req_hash
        status, body = await produce()
        # what the real §27 machinery KEEPS after a winning produce
        captured["kept"] = (rekey() if rekey is not None else None) or req_hash
        return status, jsonable_encoder(body)

    monkeypatch.setattr("api.routers.builds.run_idempotent", fake_run_idempotent)
    resp = client.post(_URL, headers={"Idempotency-Key": "k-r7"})
    assert resp.status_code == 202

    root = Path(get_settings().projects_dir)
    fp_old = eval_inputs_fingerprint(root, "demo", policy_fingerprint_bytes(old_cfg))
    fp_new = eval_inputs_fingerprint(root, "demo", policy_fingerprint_bytes(new_cfg))
    # the initial identity is the unlocked read (it must exist pre-produce)...
    assert captured["initial"] == request_hash("POST", _URL, fp_old.encode() + b"\0" + b"")
    # ...but the KEPT identity is the under-lock one — exactly the job's pin
    assert captured["kept"] == request_hash("POST", _URL, fp_new.encode() + b"\0" + b"")
    assert created["pinned_fingerprint"] == fp_new
