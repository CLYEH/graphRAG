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
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api.routers.projects as projects_module
from api.app import create_app
from api.deps import db_conn, db_conn_provider
from api.pagination import decode_sorted_cursor, scope_fingerprint
from api.routers.projects import _tombstone_name
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


def test_projects_and_sources_cursors_are_not_interchangeable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why (QA8/D6): MEASURED, not theorised — both listings minted an untagged
    (datetime, <id>) pair, and a /sources token decoded cleanly as a /projects
    cursor, so one project's source listing silently re-anchored the GLOBAL
    project list. A sources token also re-anchored another project's sources.

    Two distinct harms, hence two assertions: crossing listings (a page of
    projects positioned by a source row) and crossing projects (a cross-tenant
    read of a list the caller never asked for).
    """
    sid = uuid.uuid4()

    async def fake_projects(conn: Any, *, limit: int, after: Any = None) -> Any:
        return [_PROJECT], (_TS, "p")

    async def fake_sources(conn: Any, project: str, *, limit: int, after: Any = None) -> Any:
        return [_SOURCE], (_TS, sid)

    async def present(conn: Any, name: str) -> Any:
        return _PROJECT

    _stub(monkeypatch, "projects", "list_projects", fake_projects)
    _stub(monkeypatch, "sources", "list_sources", fake_sources)
    _stub(monkeypatch, "sources", "get_project", present)

    proj_token = client.get("/projects").json()["meta"]["next_cursor"]
    src_token = client.get("/projects/p/sources").json()["meta"]["next_cursor"]

    # (a) each token carries its own listing's tag
    proj_tag = f"created_at:desc|{scope_fingerprint('projects', '', None, {})}"
    src_tag = f"added_at:desc|{scope_fingerprint('sources', 'p', None, {})}"
    assert decode_sorted_cursor(proj_token, proj_tag, (datetime, str)) == (_TS, "p")
    assert decode_sorted_cursor(src_token, src_tag, (datetime, uuid.UUID)) == (_TS, sid)

    # (b) the measured interchange is refused in the direction it worked
    assert client.get("/projects", params={"cursor": src_token}).status_code == 400
    # ...and a sources cursor no longer anchors ANOTHER project's sources
    assert client.get("/projects/q/sources", params={"cursor": src_token}).status_code == 400

    # (c) over-block dual: each still pages its own listing
    assert client.get("/projects", params={"cursor": proj_token}).status_code == 200
    assert client.get("/projects/p/sources", params={"cursor": src_token}).status_code == 200


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("name", "a\x00b", "NUL cannot live in a Postgres text column"),
        ("name", "   ", "minLength:1 is satisfied by whitespace, but a blank key names nothing"),
        ("name", ".", "a dot segment is normalized away before routing"),
        ("name", "..", "a dot segment is normalized away before routing"),
        ("name", "a/b", "%2F decodes back to / and misses the single-segment route"),
        # Codex #149: the FILESYSTEM half of the same defect. These pass the
        # REST-addressability rule but `safe_project_subdir` rejects them, so
        # the project was created and then every filesystem-backed feature
        # broke for it — uploads 400, eval preflight failed — with the row
        # already committed. One shared component rule now serves both.
        ("name", "a\\b", "backslash is a separator on Windows and aliases on a shared store"),
        ("name", "a\\..\\b", "backslash traversal"),
        ("name", "a:b", "a colon is a drive/stream separator"),
        ("name", "C:evil", "drive-relative paths escape the corpus root"),
        # Codex #149 round 2 — the THIRD surface and two aliasing shapes.
        # "a|b" is a safe path component whose corpus as_uri() encodes to a
        # form the source resolver rejects, so the project was creatable and
        # then every upload 400'd forever. "p " / "p." are stripped by Windows
        # mkdir, so p, "p " and "p." would share ONE corpus dir — one
        # project's uploads become another's documents, and deleting any of
        # them removes the others' files.
        ("name", "a|b", "as_uri() encodes to a form no build can resolve"),
        ("name", "p ", "Windows strips the trailing space -> aliases onto p"),
        ("name", "p.", "Windows strips the trailing dot -> aliases onto p"),
        # Codex #149 r4 — the FOURTH surface: nothing above creates a
        # directory, so an over-long name passed every check and the FIRST
        # upload died in mkdir. Both limits apply because they disagree and a
        # store can be shared across platforms: NTFS counts 255 UTF-16 units
        # (200 CJK chars succeed here), ext4 counts 255 BYTES (the same name
        # is 600 and fails there).
        ("name", "x" * 256, "past the single-component limit on every filesystem"),
        ("name", "海" * 90, "270 UTF-8 bytes — fits NTFS, breaks ext4"),
    ],
)
def test_create_refuses_a_name_the_rest_of_the_system_cannot_carry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, field: str, value: str, why: str
) -> None:
    """Why (QA10): these all returned 201 and reached the registry.

    Two distinct harms, both measured. A NUL or unpaired surrogate is legal
    JSON that Postgres cannot store, so the write failed later in the driver
    with a low-level error naming no cause — a 500 for what is a client error.
    A name that is not a usable path segment is worse than an error: the
    project was CREATED and then unreachable, because every subsequent
    `GET/PATCH/DELETE /projects/{name}` 404s — including the delete that would
    remove it. Creation is the only point where the caller can still be told.

    `core/mcp/server.py` already described its own guard as deferring to
    "the guard the WRITE path uses"; until now the REST write path had none.
    """

    async def must_not_run(conn: Any, **kw: Any) -> Project:
        raise AssertionError(f"the registry must not be reached: {why}")

    _stub(monkeypatch, "projects", "create_project", must_not_run)
    r = client.post("/projects", json={field: value})

    assert r.status_code == 400, (field, value)
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_refuses_unstorable_strings_anywhere_in_the_config_bag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config is an OPEN bag, so a bad string can hide in a KEY as well as a
    value — Postgres rejects both, so both are refused here (the shared
    `unstorable_string_reason` scans keys for exactly this reason)."""

    async def must_not_run(conn: Any, **kw: Any) -> Project:
        raise AssertionError("the registry must not be reached")

    _stub(monkeypatch, "projects", "create_project", must_not_run)

    # NUL only here — a lone surrogate cannot be sent through `json=` at all
    # (httpx raises before the request leaves), so it has its own raw-body test
    for config in ({"a\x00b": 1}, {"k": "v\x00w"}, {"k": {"nested": {"deep": "x\x00y"}}}):
        r = client.post("/projects", json={"name": "p", "config": config})
        assert r.status_code == 400, config
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "raw",
    [
        '{"name": "a\\ud800b"}',
        '{"name": "p", "config": {"k": "v\\udfffw"}}',
        '{"name": "p", "config": {"a\\ud800b": 1}}',
    ],
)
def test_create_refuses_an_escaped_lone_surrogate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Why (QA10): a lone surrogate has to be sent as a JSON ESCAPE, not as a
    Python string — an HTTP client cannot encode one directly (httpx raises
    before the request leaves), which is exactly why this needs a raw body and
    why it is easy to leave untested.

    `json.loads` materializes `\\ud800` as a surrogate code point (paired
    escapes combine into the astral character at parse time, so any surrogate
    REMAINING after parsing is unpaired), and no UTF-8 encoder — Postgres
    included — can encode it. It used to reach the registry and fail in the
    driver with a low-level error naming no cause.
    """

    async def must_not_run(conn: Any, **kw: Any) -> Project:
        raise AssertionError("the registry must not be reached")

    _stub(monkeypatch, "projects", "create_project", must_not_run)

    r = client.post("/projects", content=raw, headers={"Content-Type": "application/json"})

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_body_too_deep_to_validate_is_refused_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why (QA10): a deeply-nested config produced INTERNAL, and the last step
    was the one that broke — a validation error's `details` embed the OFFENDING
    INPUT, so encoding the refusal recursed as far as validating it did. The
    path whose whole job is to answer client errors honestly was itself
    turning a 400 into a 500.

    Measured window: 1000 deep produced INTERNAL while 20000 deep was already
    refused by the JSON parser — deep enough to get past parsing, deep enough
    to crash the reply. The contract sets no nesting policy, so the bound here
    is the interpreter's own limit: exactly what cannot be processed is
    refused, and choosing a stricter cap is left to whoever sets that policy.
    """

    async def must_not_run(conn: Any, **kw: Any) -> Project:
        raise AssertionError("the registry must not be reached")

    _stub(monkeypatch, "projects", "create_project", must_not_run)
    body = '{"name":"p","config":' + '{"a":' * 1200 + "null" + "}" * 1200 + "}"

    r = client.post("/projects", content=body, headers={"Content-Type": "application/json"})

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # the envelope survives even though the details could not be encoded
    assert r.json()["error"]["request_id"]


def test_sources_refuse_the_same_unstorable_strings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same predicate on the sibling write path — one implementation for
    every facet, so the two cannot drift into accepting different sets."""

    async def must_not_run(conn: Any, project: str, **kw: Any) -> Any:
        raise AssertionError("the registry must not be reached")

    _stub(monkeypatch, "sources", "add_source", must_not_run)

    for body in (
        {"uri": "a\x00b"},
        {"uri": "   "},
        {"uri": "u", "metadata": {"k": "v\x00w"}},
        {"uri": "u", "metadata": {"nested": {"k": "v\x00w"}}},
    ):
        r = client.post("/projects/p/sources", json=body)
        assert r.status_code == 400, body
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_legitimate_unicode_name_still_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over-block dual. The refusals above are about what the STORE and the
    ROUTER cannot carry, not about restricting names to ASCII — a Chinese
    project name, spaces, and a '%' all round-trip through the path encoding
    and must keep working."""
    created: list[str] = []

    async def fake_create(conn: Any, **kw: Any) -> Project:
        created.append(kw["name"])
        return _PROJECT

    _stub(monkeypatch, "projects", "create_project", fake_create)

    for name in ("海科館", "my project", "100% real", "a.b", "..leading"):
        r = client.post("/projects", json={"name": name})
        assert r.status_code == 201, name
    assert created == ["海科館", "my project", "100% real", "a.b", "..leading"]


@pytest.mark.parametrize(
    "raw",
    [
        '{"name": "p", "config": {"x": NaN}}',
        '{"name": "p", "config": {"x": Infinity}}',
        '{"name": "p", "config": {"x": -Infinity}}',
        '{"name": "p", "config": {"x": 1e999}}',
        '{"name": "p", "config": {"nested": {"deep": [1, NaN]}}}',
    ],
)
def test_create_refuses_non_finite_numbers_in_the_config_bag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Why (QA10): the NUMBER facet of the same JSON-valid-but-JSONB-unstorable
    class as NUL and surrogates — this module's own docstring already names
    them as one class, and closing only the string half left the other open.

    SQLAlchemy's default ``json_serializer=json.dumps`` emits a literal ``NaN``
    for a non-finite float, which Postgres rejects — so it passed every shape
    check here and failed the write later with a low-level error naming no
    cause. ``1e999`` is included deliberately: it is a token
    ``parse_constant`` never sees, because it has already OVERFLOWED to ``inf``
    by the time anything can inspect it.

    The parse-time hooks that close this for the uploads/sidecar paths cannot
    run here — FastAPI parses the body, so no ``json.loads`` of ours is
    involved — which is why the refusal has to be a walk at this boundary.
    """

    async def must_not_run(conn: Any, **kw: Any) -> Project:
        raise AssertionError("the registry must not be reached")

    _stub(monkeypatch, "projects", "create_project", must_not_run)

    r = client.post("/projects", content=raw, headers={"Content-Type": "application/json"})

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_ordinary_numbers_still_store(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Over-block dual for the non-finite guard: it must refuse what Postgres
    cannot hold, not numbers in general — including a very large FINITE float,
    which is the nearest neighbour to the overflow case."""
    stored: list[Any] = []

    async def fake_create(conn: Any, **kw: Any) -> Project:
        stored.append(kw.get("config"))
        return _PROJECT

    _stub(monkeypatch, "projects", "create_project", fake_create)

    for raw in (
        '{"name": "p", "config": {"x": 1.5}}',
        '{"name": "p", "config": {"x": 0}}',
        '{"name": "p", "config": {"x": 1e300}}',
        '{"name": "p", "config": {"x": -2.5e-9}}',
    ):
        r = client.post("/projects", content=raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 201, raw
    assert len(stored) == 4


def test_a_review_decision_reason_is_guarded_like_every_stored_column(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why (QA10): ``reason`` is PERSISTED — this DTO's own docstring says it
    lands on both the ledger entry and the candidate row — but it was a bare
    ``str | None``, so a NUL went straight into the Postgres write.

    One DTO serves all twelve review-decision endpoints named in the task, so
    the guard is one line; the point of the test is that the guard is on the
    SHARED type, not on one endpoint's handler.
    """
    from api.schemas import ReviewDecisionRequest

    for bad in ("a\x00b", "   \x00"):
        with pytest.raises(ValidationError):
            ReviewDecisionRequest(reason=bad)

    # over-block dual: an ordinary reason, and an omitted one, still validate
    assert ReviewDecisionRequest(reason="duplicate of #4").reason == "duplicate of #4"
    assert ReviewDecisionRequest().reason is None
    assert ReviewDecisionRequest(reason=None).reason is None


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


def test_a_maximum_length_project_can_still_be_deleted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Why (Codex #149 r4): the delete path DERIVES a name from the key —
    ``.deleting-{name}-{32 hex}``, 43 characters longer than the key it is
    built from. The new 255-byte limit on the key therefore permitted a
    298-character tombstone, and the rename raised INSIDE the deleting
    transaction: measured, a 213-character name gave OSError, the transaction
    rolled back, and the project became **permanently undeletable** — a 500 on
    every retry, for a condition its own key caused.

    The lesson generalises past this one format: **a bound on a value must be a
    bound on the longest thing the system builds from it.** The fix bounds the
    tombstone by construction (truncating the readability hint; uniqueness was
    always the hex) rather than budgeting the key's limit against this string,
    which would make the project-name rule a function of an unrelated module's
    naming choice.
    """
    # ASTRAL-PLANE, not ASCII (Codex #149 r5): with "x" * 255 the byte, code
    # point and UTF-16 counts all coincide, so the test structurally cannot
    # fail on the unit class no matter how the truncation is written. 63 emoji
    # is a creatable 252-BYTE key whose tombstone ran to 295 bytes when the
    # hint was sliced by code points — over the 255-byte limit ext4/APFS
    # enforce, and invisible to a Windows probe because NTFS counts UTF-16.
    name = "😀" * 63
    corpus = tmp_path / name
    corpus.mkdir()

    monkeypatch.setattr(
        projects_module,
        "get_settings",
        lambda: SimpleNamespace(upload_corpus_dir=str(tmp_path)),
    )

    async def ok(conn: Any, project: str) -> bool:
        return True

    _stub(monkeypatch, "projects", "delete_project", ok)

    r = client.delete(f"/projects/{name}")

    assert r.status_code == 204, r.json() if r.content else r.status_code
    assert not corpus.exists()  # detached and cleaned, not stranded

    # The BYTE invariant, asserted directly. The rename above cannot carry this
    # test on its own: NTFS counts UTF-16 units, so a 295-byte tombstone renames
    # fine on Windows and the behavioural assertion passes either way (probed —
    # reverting the byte-slice leaves it green here). ext4/APFS count bytes, so
    # the defect would only appear in CI. The invariant is platform-independent,
    # so assert THAT and the test discriminates everywhere it runs.
    for key in ("😀" * 63, "海" * 85, "x" * 255):
        assert len(_tombstone_name(key).encode("utf-8")) <= 255, key[:8]


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
    assert tomb is not None and not (tmp_path / "demo").exists()  # the corpus moved aside
    assert tomb.exists() and (tomb / "doc.txt").exists()  # detached, not yet removed
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


def test_a_cancelled_delete_gives_the_corpus_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation must not strand the corpus under a tombstone (Codex #145).

    The detach is deliberately synchronous, so the interleaving that would
    strand it — rename lands, awaiter never learns the path — cannot exist:
    no await separates the rename from the assignment, so CancelledError
    cannot be delivered between them. Threading it made that racy rather than
    safe, because the awaiter resumes on cancellation WITHOUT waiting for the
    thread.

    What remains reachable is cancellation at a real await, i.e. while the
    transaction is completing. That must still hand the corpus back, and it
    exercises the synchronous re-attach — the compensating action that has to
    run when awaiting is no longer reliable.
    """
    import asyncio as _asyncio

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "doc.txt").write_text("registered bytes", encoding="utf-8")

    class _Settings:
        upload_corpus_dir = str(tmp_path)

    monkeypatch.setattr("api.routers.projects.get_settings", lambda: _Settings())

    async def _deleted(conn: Any, name: str) -> bool:
        return True

    _stub(monkeypatch, "projects", "delete_project", _deleted)

    @asynccontextmanager
    async def _cancelled_on_exit() -> AsyncIterator[object]:
        yield object()
        raise _asyncio.CancelledError

    # driven directly rather than through the transport: CancelledError is a
    # BaseException, and the point is that the handler catches BaseException
    # rather than Exception — routed through TestClient it would surface as a
    # transport error and stop testing that distinction
    async def _drive() -> None:
        await projects_module.delete_project_endpoint(lambda: _cancelled_on_exit(), "demo")  # type: ignore[arg-type,return-value]

    with pytest.raises(_asyncio.CancelledError):
        _asyncio.run(_drive())

    assert (tmp_path / "demo" / "doc.txt").exists(), "a cancelled delete must give the corpus back"
    assert not any(p.name.startswith(".deleting-") for p in tmp_path.iterdir())
