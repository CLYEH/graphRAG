"""Why: Qdrant keeps every build's points in ONE per-project collection (§4),
so DR-006's "never mix versions" rests on this layer sending the scope filter
with every read and stamping the scope into every written payload. Unlike SQL
or Cypher there is no query text — the residual escape surface is a
caller-supplied Filter object, and these tests pin that no such parameter
exists and that the only filter ever sent is the module's own, scope-first.
Execution against a live server is covered by the integration tests.
"""

from __future__ import annotations

import inspect
import re
import uuid
from typing import Any, cast

import pytest
from qdrant_client import AsyncQdrantClient, models
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import vectors as vectors_module
from core.stores.repo import BuildNotWritableError
from core.stores.vectors import (
    BuildScopedVectorProjector,
    BuildScopedVectorRepo,
    collection_for,
)

_BUILD = uuid.uuid4()


class _FakeClient:
    """Captures every Qdrant call so the scope-injection contract can be
    pinned without a server; returns canned responses."""

    def __init__(self, points: list[Any] | None = None, count: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._points = points or []
        self._count = count

    async def query_points(self, collection_name: str, **kwargs: Any) -> Any:
        self.calls.append(("query_points", {"collection_name": collection_name, **kwargs}))
        return type("R", (), {"points": self._points})()

    async def count(self, collection_name: str, **kwargs: Any) -> Any:
        self.calls.append(("count", {"collection_name": collection_name, **kwargs}))
        return type("R", (), {"count": self._count})()

    async def collection_exists(self, collection_name: str) -> bool:
        self.calls.append(("collection_exists", {"collection_name": collection_name}))
        return False

    async def create_collection(self, collection_name: str, **kwargs: Any) -> bool:
        self.calls.append(("create_collection", {"collection_name": collection_name, **kwargs}))
        return True

    async def upsert(self, collection_name: str, **kwargs: Any) -> Any:
        self.calls.append(("upsert", {"collection_name": collection_name, **kwargs}))
        return None


class _FakePGConn:
    """Answers the projector's status revalidation with a fixed status."""

    def __init__(self, status: str | None) -> None:
        self._status = status

    async def execute(self, statement: object) -> _FakePGConn:
        return self

    def scalar_one_or_none(self) -> str | None:
        return self._status


def _repo(client: _FakeClient | None = None) -> BuildScopedVectorRepo:
    return BuildScopedVectorRepo(
        cast(AsyncQdrantClient, client or _FakeClient()),
        "p1",
        _BUILD,
        _token=vectors_module._CONSTRUCTION_TOKEN,
    )


def _projector(
    client: _FakeClient | None = None, status: str | None = "building"
) -> BuildScopedVectorProjector:
    return BuildScopedVectorProjector(
        cast(AsyncConnection, _FakePGConn(status)),
        cast(AsyncQdrantClient, client or _FakeClient()),
        "p1",
        _BUILD,
        _token=vectors_module._CONSTRUCTION_TOKEN,
    )


def _scope_of(filter_: models.Filter) -> dict[str, Any]:
    assert isinstance(filter_.must, list)
    return {
        cond.key: cond.match.value
        for cond in filter_.must
        if isinstance(cond, models.FieldCondition) and isinstance(cond.match, models.MatchValue)
    }


def test_the_scope_filter_is_scope_first_and_string_typed() -> None:
    """Payloads store build_id as a string (uuid is not a JSON type) — a
    UUID-typed filter value would silently match nothing (false-empty). And
    the scope lives in a top-level `must`, so any extra typed condition can
    only narrow."""
    plain = _repo()._scope_filter()
    assert _scope_of(plain) == {"project": "p1", "build_id": str(_BUILD)}
    assert plain.should is None and plain.must_not is None
    typed = _repo()._scope_filter("chunk")
    assert _scope_of(typed) == {"project": "p1", "build_id": str(_BUILD), "type": "chunk"}


def test_public_surface_accepts_no_filter_objects() -> None:
    """Qdrant's escape surface is not query text but the Filter object — a
    caller-supplied Filter could `should` its way around a naive merge. The
    pin: no public method takes a filter/conditions parameter, and the
    public surface itself is frozen by name (adding one later is a loud,
    reviewed decision)."""
    reader_public = {name for name in dir(BuildScopedVectorRepo) if not name.startswith("_")}
    assert reader_public == {
        "project",
        "build_id",
        "for_active_build",
        "search",
        "point_count",
    }
    projector_public = {
        name for name in dir(BuildScopedVectorProjector) if not name.startswith("_")
    }
    assert projector_public == reader_public | {
        "for_building_build",
        "ensure_collection",
        "upsert_point",
    }
    for cls in (BuildScopedVectorRepo, BuildScopedVectorProjector):
        for name in (n for n in dir(cls) if not n.startswith("_")):
            member = inspect.getattr_static(cls, name)
            if isinstance(member, property):
                continue
            func = member.__func__ if isinstance(member, classmethod) else member
            params = set(inspect.signature(func).parameters)
            assert not params & {
                "filter",
                "query_filter",
                "count_filter",
                "conditions",
                "payload",
            }, (cls, name)


async def test_reads_send_the_scope_filter_and_nothing_else() -> None:
    """Every read carries the module's own filter — the scope reaches the
    server on every call, not just on polite ones."""
    client = _FakeClient(points=[object()], count=3)
    repo = _repo(client)
    assert len(await repo.search([0.1, 0.2], limit=5)) == 1
    assert await repo.point_count("entity") == 3
    (search_name, search_kwargs), (count_name, count_kwargs) = client.calls
    assert (search_name, count_name) == ("query_points", "count")
    assert search_kwargs["collection_name"] == collection_for("p1")
    assert _scope_of(search_kwargs["query_filter"]) == {"project": "p1", "build_id": str(_BUILD)}
    assert search_kwargs["limit"] == 5
    assert _scope_of(count_kwargs["count_filter"])["type"] == "entity"
    assert count_kwargs["exact"] is True


async def test_upserts_stamp_the_bound_scope_into_the_payload() -> None:
    """§4 payload shape with the scope set BY THE BINDING — there is no
    caller payload dict, so a foreign project/build_id is unrepresentable."""
    client = _FakeClient()
    point = uuid.uuid4()
    await _projector(client).upsert_point(
        point,
        [0.1, 0.2],
        canonical_id="e-1",
        point_type="entity",
        text="Alice",
        entity_id="e-1",
    )
    (_, kwargs) = client.calls[-1]
    (struct,) = kwargs["points"]
    assert struct.id == str(point)
    assert struct.payload["project"] == "p1"
    assert struct.payload["build_id"] == str(_BUILD)
    assert struct.payload["canonical_id"] == "e-1"
    assert struct.payload["type"] == "entity"
    assert kwargs["wait"] is True  # §5 read-after-write for skip/rerun decisions


async def test_every_write_revalidates_the_building_status_first() -> None:
    """§27.1 per write, not per binding (the cross-store TOCTOU): a build that
    stopped being `building` refuses BEFORE any Qdrant call is made — and
    that covers ensure_collection too, because creation freezes the shared
    collection's vector schema (a stale projector creating it with the wrong
    size would break the next build's indexing); the write license expiring
    stops ALL side effects, not just build-tagged payloads."""
    client = _FakeClient()
    stale = _projector(client, status="active")
    with pytest.raises(BuildNotWritableError) as excinfo:
        await stale.upsert_point(
            uuid.uuid4(), [0.1], canonical_id="c", point_type="chunk", text="t"
        )
    assert excinfo.value.status == "active"
    with pytest.raises(BuildNotWritableError):
        await stale.ensure_collection(4)
    assert client.calls == []  # refused before touching the vector store


def test_consumers_cannot_reach_the_client_or_mutate_the_scope() -> None:
    """DR-006's fence, same as the sibling repos: no raw client on the public
    surface, no setters, no __dict__ to smuggle state into."""
    repo = _repo()
    with pytest.raises(AttributeError):
        _ = repo.client  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        repo.build_id = uuid.uuid4()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        repo.project = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        repo.escape_hatch = object()  # type: ignore[attr-defined]


def test_direct_construction_is_fenced_off() -> None:
    """Factories are the only sanctioned bindings — for_active_build resolves
    the scope from Postgres (DR-001), for_building_build validates it."""
    with pytest.raises(TypeError, match="for_active_build"):
        BuildScopedVectorRepo(cast(AsyncQdrantClient, object()), "p1", uuid.uuid4())


def test_active_bound_repos_cannot_write() -> None:
    """§27.1: the active build is an immutable live snapshot — the READ type
    has no write methods, and the writer inherits (never overrides) the read
    factory whose return type is pinned to the read-only class."""
    assert not hasattr(_repo(), "upsert_point")
    assert not hasattr(_repo(), "ensure_collection")
    assert hasattr(_projector(), "upsert_point")
    assert "for_active_build" not in vars(BuildScopedVectorProjector)


def test_collection_names_are_derived_safe_and_collision_free() -> None:
    """The project contract (P0) only requires a non-empty string, but Qdrant
    collection names are URL-path identifiers — a raw mapping would make
    contract-valid projects (slashes, '?', unicode, very long names)
    unindexable. The derived name must be deterministic, restricted to safe
    characters, bounded in length, and distinct even when sanitization would
    collide two different projects."""
    assert collection_for("acme") == collection_for("acme")  # deterministic
    hostile = ["ab/c", "ab?c", "ab*c", "a" * 300, "專案", "a b"]
    for project in hostile:
        name = collection_for(project)
        assert re.fullmatch(r"[A-Za-z0-9_-]+", name), project
        assert len(name) <= 64, project
    # sanitization alone would collide these — the content hash keeps them apart
    assert collection_for("ab/c") != collection_for("ab_c")
    assert collection_for("ab/c") != collection_for("ab?c")
    # the readable prefix survives for debuggability
    assert collection_for("acme").startswith("project_acme_")
