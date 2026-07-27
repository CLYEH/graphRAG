"""Why: the routers own real logic above the registry — status codes, the §15
envelope, opaque-cursor plumbing, and the domain→frozen-code error mapping —
that must hold independently of Postgres. These component tests stub the
registry layer (so no DB) and drive the handlers through the real app (real
middleware, exception handlers, param binding), pinning that orchestration; the
live SQL behavior is the integration suite's job. Idempotency-wrapped POSTs are
exercised WITHOUT a key here (the keyed path runs SQL and is integration-only).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import api.routers.projects as projects_module
from api.app import create_app
from api.deps import db_conn, db_conn_provider
from core.registry import (
    MANAGED_FILES_KEY,
    Project,
    ProjectExistsError,
    ProjectHasBuildsError,
    ProjectNotFoundError,
    Source,
    SourceNotFoundError,
)

pytestmark = pytest.mark.contract

_TS = datetime(2026, 7, 7, tzinfo=UTC)
_PROJECT = Project(name="p", display_name="D", description=None, config={}, created_at=_TS)
_SOURCE = Source(id=uuid.uuid4(), project="p", kind="file", uri="u", metadata={}, added_at=_TS)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()  # registry is stubbed; the connection is never used

    @asynccontextmanager
    async def _open() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[db_conn] = _conn
    # BOTH must be overridden: a handler that owns its transaction resolves
    # db_conn_provider instead, and leaving it unbound would silently hand
    # this contract-tier file a REAL engine.connect() — a hidden live-DB
    # dependency that passes wherever Postgres happens to be up and fails in
    # CI's service-less backend job (the delete endpoint did exactly this).
    app.dependency_overrides[db_conn_provider] = lambda: _open
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, module: str, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.{module}.{name}", fn)


def test_list_projects_envelope_and_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(conn: Any, *, limit: int, after: Any = None) -> Any:
        return [_PROJECT], (_TS, "p")  # a next page remains

    _stub(monkeypatch, "projects", "list_projects", fake_list)
    r = client.get("/projects")
    assert r.status_code == 200
    body = r.json()
    assert body["data"][0]["name"] == "p"
    assert body["meta"]["next_cursor"]  # encoded from the (ts, name) keyset
    assert body["meta"]["build_id"] is None


def test_create_project_201_without_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_create(conn: Any, **kw: Any) -> Project:
        return _PROJECT

    _stub(monkeypatch, "projects", "create_project", fake_create)
    r = client.post("/projects", json={"name": "p"})
    assert r.status_code == 201
    assert r.json()["data"]["name"] == "p"


def test_create_duplicate_maps_to_validation_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_create(conn: Any, **kw: Any) -> Project:
        raise ProjectExistsError("p")

    _stub(monkeypatch, "projects", "create_project", fake_create)
    r = client.post("/projects", json={"name": "p"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["details"]["name"] == "p"


def test_get_project_404_and_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "projects", "get_project", missing)
    assert client.get("/projects/x").status_code == 404

    async def present(conn: Any, name: str) -> Project:
        return _PROJECT

    _stub(monkeypatch, "projects", "get_project", present)
    r = client.get("/projects/p")
    assert r.status_code == 200
    assert r.json()["data"]["display_name"] == "D"


def test_update_project_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(conn: Any, name: str, **kw: Any) -> None:
        return None

    _stub(monkeypatch, "projects", "update_project", missing)
    assert client.patch("/projects/x", json={"description": "d"}).status_code == 404


def test_delete_project_204_and_has_builds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(conn: Any, name: str) -> bool:
        return True

    async def _gone(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "projects", "delete_project", ok)
    # the post-commit re-check (Codex #145 P1) consults the SoR before cleanup
    _stub(monkeypatch, "projects", "get_project", _gone)
    r = client.delete("/projects/p")
    assert r.status_code == 204
    assert r.content == b""

    async def has_builds(conn: Any, name: str) -> bool:
        raise ProjectHasBuildsError("p", 2)

    _stub(monkeypatch, "projects", "delete_project", has_builds)
    r = client.delete("/projects/p")
    assert r.status_code == 400
    assert r.json()["error"]["details"]["builds"] == 2


def test_delete_project_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def gone(conn: Any, name: str) -> bool:
        return False

    _stub(monkeypatch, "projects", "delete_project", gone)
    assert client.delete("/projects/x").status_code == 404


def test_list_sources_404_when_project_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(conn: Any, name: str) -> None:
        return None

    _stub(monkeypatch, "sources", "get_project", missing)
    assert client.get("/projects/x/sources").status_code == 404


def test_add_source_201_and_missing_project(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def add_ok(conn: Any, project: str, **kw: Any) -> Source:
        return _SOURCE

    _stub(monkeypatch, "sources", "add_source", add_ok)
    r = client.post("/projects/p/sources", json={"uri": "u"})
    assert r.status_code == 201
    assert "project" not in r.json()["data"]

    async def add_missing(conn: Any, project: str, **kw: Any) -> Source:
        raise ProjectNotFoundError("x")

    _stub(monkeypatch, "sources", "add_source", add_missing)
    r = client.post("/projects/x/sources", json={"uri": "u"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_add_source_rejects_reserved_managed_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (triage 26): MANAGED_FILES_KEY is a reserved server-owned metadata key whose
    # presence marks an upload-managed source. A client that set it via POST /sources
    # would SPOOF a managed source — an ordinary directory then ingests only the listed
    # names (or nothing for {}), or fails the build on a malformed value. The endpoint
    # rejects it as a 400 BEFORE add_source runs, so the store never stores the spoof.
    called = {"n": 0}

    async def add_should_not_run(conn: Any, project: str, **kw: Any) -> Source:
        called["n"] += 1
        return _SOURCE

    _stub(monkeypatch, "sources", "add_source", add_should_not_run)
    r = client.post(
        "/projects/p/sources",
        json={"uri": "file:///d", "kind": "text", "metadata": {MANAGED_FILES_KEY: {}}},
    )
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["reserved_key"] == MANAGED_FILES_KEY
    assert called["n"] == 0  # rejected before touching the store


def _present_project(monkeypatch: pytest.MonkeyPatch) -> None:
    async def present(conn: Any, name: str) -> Project:
        return _PROJECT

    _stub(monkeypatch, "sources", "get_project", present)


def test_update_source_200_disables_and_echoes_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SRC2: PATCH sets enabled and returns the updated source; the DTO must
    # carry `enabled` so the Console can reflect the disabled state.
    _present_project(monkeypatch)
    seen: dict[str, Any] = {}

    async def update_ok(conn: Any, project: str, source_id: Any, *, enabled: bool) -> Source:
        seen["enabled"] = enabled
        return Source(
            id=source_id,
            project=project,
            kind="file",
            uri="u",
            metadata={},
            added_at=_TS,
            enabled=enabled,
        )

    _stub(monkeypatch, "sources", "update_source", update_ok)
    sid = str(uuid.uuid4())
    r = client.patch(f"/projects/p/sources/{sid}", json={"enabled": False})
    assert r.status_code == 200
    assert seen["enabled"] is False  # the body value reached the store call
    data = r.json()["data"]
    assert data["enabled"] is False
    assert "project" not in data  # path context, not echoed (sibling convention)


def test_update_source_404_when_source_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a project that EXISTS but has no such source is SOURCE_NOT_FOUND (the new
    # v1.3 code), distinct from PROJECT_NOT_FOUND — the client can tell which
    # half of the path was wrong.
    _present_project(monkeypatch)

    async def update_missing(conn: Any, project: str, source_id: Any, *, enabled: bool) -> Source:
        raise SourceNotFoundError(project, source_id)

    _stub(monkeypatch, "sources", "update_source", update_missing)
    r = client.patch(f"/projects/p/sources/{uuid.uuid4()}", json={"enabled": True})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_update_source_404_when_project_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a missing PROJECT is PROJECT_NOT_FOUND, pre-checked before the source
    # update runs (sibling-endpoint convention) — never a misleading 404 code.
    async def missing(conn: Any, name: str) -> None:
        return None

    ran = {"n": 0}

    async def update_should_not_run(
        conn: Any, project: str, source_id: Any, *, enabled: bool
    ) -> Source:
        ran["n"] += 1
        return _SOURCE

    _stub(monkeypatch, "sources", "get_project", missing)
    _stub(monkeypatch, "sources", "update_source", update_should_not_run)
    r = client.patch(f"/projects/x/sources/{uuid.uuid4()}", json={"enabled": False})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert ran["n"] == 0  # pre-check short-circuits before the store call


def test_update_source_rejects_unknown_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SRC2 immutability is structural: uri/kind are not in SourceUpdate, and
    # extra="forbid" (the contract's additionalProperties:false) makes a
    # smuggled `uri` a 422 — never a silently-ignored no-op.
    _present_project(monkeypatch)

    async def update_should_not_run(
        conn: Any, project: str, source_id: Any, *, enabled: bool
    ) -> Source:
        raise AssertionError("must not reach the store on an invalid body")

    _stub(monkeypatch, "sources", "update_source", update_should_not_run)
    r = client.patch(
        f"/projects/p/sources/{uuid.uuid4()}", json={"enabled": False, "uri": "file:///new"}
    )
    # the app reshapes FastAPI's body-validation 422 into the frozen envelope
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("bad", ["false", "0", 0, 1, "true"])
def test_update_source_rejects_non_boolean_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    # SRC2: the contract types `enabled` a JSON boolean; strict validation must
    # reject a coercible non-boolean (`"false"`/`0`/…) rather than silently
    # flip the source's state on a malformed payload — the runtime boundary
    # equals the contract, not Pydantic's lax default.
    _present_project(monkeypatch)

    async def update_should_not_run(
        conn: Any, project: str, source_id: Any, *, enabled: bool
    ) -> Source:
        raise AssertionError("must not reach the store on a non-boolean enabled")

    _stub(monkeypatch, "sources", "update_source", update_should_not_run)
    r = client.patch(f"/projects/p/sources/{uuid.uuid4()}", json={"enabled": bad})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_deleting_a_project_removes_its_uploads_without_escaping_the_corpus_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted project must not leave its managed corpus on disk (QA7/D16).

    DELETE only removed the registry row, so uploaded documents outlived the
    project with no endpoint able to list or remove them — unbounded growth
    and an unintended retention surface. The cleanup runs AFTER the row
    delete, which is deliberately the opposite of prune's "projections first,
    Postgres last": that rule assumes the truth-delete always proceeds, while
    this one can legitimately REFUSE (builds present, active jobs), and
    removing a caller's documents before a refusal would destroy data on a
    request the server then rejects.

    The path is resolved through the same containment guard the upload writer
    uses, so a crafted project name cannot direct the delete outside the
    corpus root — pinned here because this is the first code that DELETES
    inside that root, and a traversal bug would be silent and unrecoverable.
    """
    from api.routers.projects import _detach_upload_dir

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "doc.txt").write_text("uploaded", encoding="utf-8")
    (tmp_path / "neighbour").mkdir()
    (tmp_path / "neighbour" / "keep.txt").write_text("not mine", encoding="utf-8")

    class _Settings:
        upload_corpus_dir = str(tmp_path)

    monkeypatch.setattr("api.routers.projects.get_settings", lambda: _Settings())

    tomb = _detach_upload_dir("demo")
    assert tomb is not None and not (tmp_path / "demo").exists()
    assert (
        tomb.exists() and (tomb / "doc.txt").exists()
    )  # detached, not yet removed  # the project's corpus is gone
    assert (tmp_path / "neighbour" / "keep.txt").exists()  # nobody else's is

    assert _detach_upload_dir("never-uploaded") is None  # never uploaded: no-op

    for crafted in ("..", "../escape", "a/b", "/abs"):
        assert _detach_upload_dir(crafted) is None
    assert (tmp_path / "neighbour").exists() and tmp_path.exists()


def test_a_later_project_reusing_the_name_is_unreachable_by_this_cleanup(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup must not be able to reach a LATER project's uploads (Codex #145).

    The corpus is addressed by name, and a name is reusable the moment its row
    is gone; multiple one-worker instances sharing a corpus root is the
    documented scaling shape. A post-commit re-check only MOVED that window —
    its SELECT's transaction ends before the rmtree, and a future INSERT has no
    row to lock. So the directory is DETACHED inside the deleting transaction,
    while the row lock still blocks a concurrent create of that name.

    This pins the property that makes the window unlosable: after the delete,
    the old name is FREE and empty, so anything a later project writes there is
    structurally out of reach of this request — which only ever removes the
    tombstone it renamed.
    """
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "old.txt").write_text("the deleted project's upload", encoding="utf-8")

    class _Settings:
        upload_corpus_dir = str(tmp_path)

    monkeypatch.setattr("api.routers.projects.get_settings", lambda: _Settings())

    async def _deleted(conn: Any, name: str) -> bool:
        return True

    _stub(monkeypatch, "projects", "delete_project", _deleted)
    assert client.delete("/projects/demo").status_code == 204
    assert not (tmp_path / "demo").exists()  # detached AND the tombstone swept
    assert not any(p.name.startswith(".deleting-") for p in tmp_path.iterdir())


def test_a_failed_commit_gives_the_corpus_back(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detaching inside the transaction owes a rollback path (Codex #145).

    The rename happens before the commit, so a transaction that does NOT commit
    would otherwise leave a project that still exists without its corpus — the
    data loss this whole change exists to avoid, arrived at from the other
    side. The handler re-attaches on any failure out of the block.
    """
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "doc.txt").write_text("must survive", encoding="utf-8")

    class _Settings:
        upload_corpus_dir = str(tmp_path)

    monkeypatch.setattr("api.routers.projects.get_settings", lambda: _Settings())

    async def _deleted(conn: Any, name: str) -> bool:
        return True

    _stub(monkeypatch, "projects", "delete_project", _deleted)

    @asynccontextmanager
    async def _failing_commit() -> AsyncIterator[object]:
        yield object()
        raise RuntimeError("commit failed")

    client.app.dependency_overrides[db_conn_provider] = lambda: _failing_commit  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError):
            client.delete("/projects/demo")
    finally:
        client.app.dependency_overrides.pop(db_conn_provider, None)  # type: ignore[attr-defined]
    assert (tmp_path / "demo" / "doc.txt").exists(), "a non-committing delete must give it back"


def test_the_corpus_is_detached_before_the_transaction_ends(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detach must happen INSIDE the deleting transaction (Codex #145 P1).

    That placement is the whole fix: while the row lock is held, no other
    instance can create this name, so renaming the directory aside there makes
    a later project's uploads structurally unreachable by this cleanup. Move
    the same rename after the block and the TOCTOU window reopens — which the
    other tests cannot see, because their outcomes are identical either way.
    So this pins the ORDER, not the outcome.
    """
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "doc.txt").write_text("x", encoding="utf-8")

    class _Settings:
        upload_corpus_dir = str(tmp_path)

    monkeypatch.setattr("api.routers.projects.get_settings", lambda: _Settings())

    async def _deleted(conn: Any, name: str) -> bool:
        return True

    _stub(monkeypatch, "projects", "delete_project", _deleted)

    events: list[str] = []
    real_detach = projects_module._detach_upload_dir

    def _spy_detach(project: str) -> Any:
        events.append("detach")
        return real_detach(project)

    monkeypatch.setattr("api.routers.projects._detach_upload_dir", _spy_detach)

    @asynccontextmanager
    async def _tracking() -> AsyncIterator[object]:
        yield object()
        events.append("txn-end")

    client.app.dependency_overrides[db_conn_provider] = lambda: _tracking  # type: ignore[attr-defined]
    try:
        assert client.delete("/projects/demo").status_code == 204
    finally:
        client.app.dependency_overrides.pop(db_conn_provider, None)  # type: ignore[attr-defined]

    assert events == ["detach", "txn-end"], f"detach must precede the commit, got {events}"


def test_a_refused_delete_leaves_the_corpus_exactly_where_it_was(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected DELETE must not cost the caller their documents.

    This is the guarantee the whole ordering argument exists to provide, and
    under the tombstone design it rests on TWO mechanisms rather than one: the
    refusal raises BEFORE the detach, and any exception out of the block
    re-attaches. It is therefore more worth pinning than when the ordering was
    the only thing carrying it — and it was silently lost in a test rewrite,
    which is exactly how a mechanism decays into prose.
    """
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "doc.txt").write_text("still mine", encoding="utf-8")

    class _Settings:
        upload_corpus_dir = str(tmp_path)

    monkeypatch.setattr("api.routers.projects.get_settings", lambda: _Settings())

    async def _refuses(conn: Any, name: str) -> bool:
        raise ProjectHasBuildsError(name, 2)

    _stub(monkeypatch, "projects", "delete_project", _refuses)
    assert client.delete("/projects/demo").status_code == 400
    assert (tmp_path / "demo" / "doc.txt").exists(), "a refused delete must keep the corpus"
    assert not any(p.name.startswith(".deleting-") for p in tmp_path.iterdir())


def test_a_cleanup_failure_answers_204_but_says_the_tombstone_survived(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete SUCCEEDED, so it must not report otherwise — but a leftover
    must not be invisible either (gate-2 on #145).

    Swallowing is right here: the row is committed, so raising would report a
    failure that did not happen and a retry would 404. What must not happen is
    the leak becoming undiscoverable, which is D16's own complaint in a
    narrower path — so the tombstone is named in a warning.
    """
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "doc.txt").write_text("x", encoding="utf-8")

    class _Settings:
        upload_corpus_dir = str(tmp_path)

    monkeypatch.setattr("api.routers.projects.get_settings", lambda: _Settings())

    async def _deleted(conn: Any, name: str) -> bool:
        return True

    _stub(monkeypatch, "projects", "delete_project", _deleted)
    # a cleanup that removes nothing, spied so the SWALLOW itself is pinned:
    # without ignore_errors the real rmtree would raise and 500 a delete that
    # actually succeeded, and a no-op stub alone would never notice the flag
    # going missing
    rmtree_calls: list[dict[str, Any]] = []

    def _spy_rmtree(path: Any, **kw: Any) -> None:
        rmtree_calls.append(kw)

    monkeypatch.setattr("api.routers.projects.shutil.rmtree", _spy_rmtree)

    # captured off the logger itself, not via caplog: global logging config is
    # shared state that another test can change, and a warning that only
    # appears when this file runs alone is not a pin
    warnings: list[str] = []
    monkeypatch.setattr(
        projects_module._log, "warning", lambda msg, *a: warnings.append(str(msg) % a)
    )
    assert client.delete("/projects/demo").status_code == 204  # the delete DID happen
    assert any(".deleting-" in w for w in warnings), warnings
    assert rmtree_calls == [{"ignore_errors": True}], rmtree_calls
