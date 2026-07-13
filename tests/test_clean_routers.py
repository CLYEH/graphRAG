"""Why: BA4's preview endpoint (contract v1.1, DR-009) exists to answer "how
would THESE parameters chunk MY document?" without a build and without writing
anything — so the behaviors under test are the ones that keep that answer
trustworthy: exactly-one-source (a request naming both or neither is a
confused question), the project-config fallback (a no-parameter preview must
show what a build would ACTUALLY do, not the engine defaults), the pair
relation the JSON Schema cannot express (0 <= overlap < max_chars → 400 with
the validator's own words), unknown-key rejection (a misspelled ``max_char``
silently previewing the wrong chunking would be trusted), the NULL-raw loud
failure (an empty preview reads as "these parameters produce no chunks" — the
exact wrong conclusion), and the purity guarantee itself (no write API is even
touched). Offsets are asserted exact because §27.4 evidence spans hang off the
same invariant the preview mirrors.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import db_conn
from core.stores.repo import NoActiveBuildError

pytestmark = pytest.mark.contract

_BUILD = uuid.uuid4()
_DOC = uuid.uuid4()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()  # binding + repo are stubbed; the connection is never used

    app.dependency_overrides[db_conn] = _conn
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.clean.{name}", fn)


def _project(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any] | None = None) -> None:
    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name, config=config or {})

    _stub(monkeypatch, "get_project", fake_get_project)


def _active(monkeypatch: pytest.MonkeyPatch, raw: str | None = "word " * 400) -> None:
    async def fake_resolve(conn: Any, project: str) -> Any:
        return SimpleNamespace(project=project, build_id=_BUILD)

    class _Repo:
        writes: list[Any] = []

        @classmethod
        def bound_to(cls, conn: Any, binding: Any) -> Any:
            return cls()

        async def fetch_all(self, table: Any, *where: Any) -> Sequence[Any]:
            return [SimpleNamespace(id=_DOC, raw=raw)]

    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)
    _stub(monkeypatch, "BuildScopedRepo", _Repo)


def _preview(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post("/projects/acme/clean/preview", json=body)


# ---- source selection: exactly one ------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},  # neither
        {"max_chars": 100},  # parameters but no source
        {"document_id": str(_DOC), "text": "hello"},  # both
    ],
)
def test_exactly_one_source_is_required(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    # Both-or-neither is a confused question: with both, WHICH text the chunks
    # describe is ambiguous and the caller would trust whichever we picked.
    _project(monkeypatch)
    r = _preview(client, body)
    assert r.status_code == 400
    assert "exactly one source" in r.json()["error"]["message"]


def test_unknown_keys_are_rejected_not_defaulted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A misspelled parameter that silently falls back to defaults would preview
    # the WRONG chunking — and the operator would write those numbers into the
    # project config trusting what they saw.
    _project(monkeypatch)
    r = _preview(client, {"text": "hello world", "max_char": 100})
    assert r.status_code == 400


# ---- the text source ---------------------------------------------------------


def test_text_source_chunks_with_exact_offsets_and_no_build(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The offsets ARE the product: §27.4 evidence spans point back into the
    # original text via raw[start:end] == chunk.text, and the preview exists so
    # an operator can see that mapping before committing to a build. build_id
    # is null — no build was involved, and meta must say so (a fabricated build
    # id would claim the preview came from stored corpus).
    _project(monkeypatch)
    text = "alpha beta gamma delta epsilon zeta eta theta"
    r = _preview(client, {"text": text, "max_chars": 20, "overlap": 5})
    assert r.status_code == 200
    payload = r.json()
    assert payload["meta"]["build_id"] is None
    chunks = payload["data"]["chunks"]
    assert len(chunks) > 1  # the parameters actually split
    for c in chunks:
        assert text[c["start_offset"] : c["end_offset"]] == c["text"]
    assert [c["ordinal"] for c in chunks] == list(range(len(chunks)))


def test_text_source_still_404s_a_missing_project(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The text source touches no build, but the endpoint is project-scoped: a
    # preview against a nonexistent project answering 200 with engine defaults
    # would report parameters for a project that isn't there.
    async def fake_get_project(conn: Any, name: str) -> Any:
        return None

    _stub(monkeypatch, "get_project", fake_get_project)
    r = _preview(client, {"text": "hello world"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_omitted_parameters_use_the_project_config_not_engine_defaults(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The no-parameter preview answers "what would MY build do?" — builds read
    # chunking from project config (core.builds.config), so the preview must
    # walk the same chain. With engine defaults (1200) this text is ONE chunk;
    # with the project's configured 20 it must split.
    _project(monkeypatch, config={"chunking": {"max_chars": 20, "overlap": 4}})
    r = _preview(client, {"text": "alpha beta gamma delta epsilon"})
    assert r.status_code == 200
    assert len(r.json()["data"]["chunks"]) > 1


def test_bool_config_values_fall_back_rather_than_coerce(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bool is an int subclass: `chunking: {max_chars: true}` would otherwise
    # preview with max_chars=1 (one chunk per character) — a config the build
    # loader rejects must not smuggle a numeric value in here.
    _project(monkeypatch, config={"chunking": {"max_chars": True, "overlap": False}})
    r = _preview(client, {"text": "short text"})
    assert r.status_code == 200
    assert len(r.json()["data"]["chunks"]) == 1  # engine default 1200, not max_chars=1


def test_explicit_parameter_overrides_config_per_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Per-field override is the comparison workflow FE2 exists for: change ONE
    # knob against the configured baseline. max_chars=1000 overrides config's
    # 20; overlap stays the configured 4 — if overlap fell back to the engine
    # default (200) instead, 200 < 1000 holds and the request would succeed
    # with the wrong overlap, so pin via the chunk shape: one chunk means
    # max_chars was overridden.
    _project(monkeypatch, config={"chunking": {"max_chars": 20, "overlap": 4}})
    r = _preview(client, {"text": "alpha beta gamma delta epsilon", "max_chars": 1000})
    assert r.status_code == 200
    assert len(r.json()["data"]["chunks"]) == 1


def test_pair_relation_rejects_with_the_validators_own_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0 <= overlap < max_chars is enforceable only at runtime (two-field
    # numeric comparison; the contract documents this) — and the message must
    # be chunk_text's own, which names both offending values.
    _project(monkeypatch)
    r = _preview(client, {"text": "hello world", "max_chars": 10, "overlap": 10})
    assert r.status_code == 400
    assert "overlap must satisfy" in r.json()["error"]["message"]


# ---- the document source -----------------------------------------------------


def test_document_source_stamps_the_active_build(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # meta.build_id is §15's "which build served this": the document's raw came
    # from the ACTIVE build, and the preview's claim is only valid against it.
    _project(monkeypatch)
    _active(monkeypatch, raw="word " * 400)
    r = _preview(client, {"document_id": str(_DOC)})
    assert r.status_code == 200
    payload = r.json()
    assert payload["meta"]["build_id"] == str(_BUILD)
    assert len(payload["data"]["chunks"]) > 1  # engine defaults split 2000 chars


def test_document_source_409s_without_an_active_build(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No active build means there IS no document to read — 409 NO_ACTIVE_BUILD,
    # the surface-consistent answer (never an empty preview).
    _project(monkeypatch)

    async def fake_resolve(conn: Any, project: str) -> Any:
        raise NoActiveBuildError(project)

    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)
    r = _preview(client, {"document_id": str(_DOC)})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_ACTIVE_BUILD"


def test_missing_document_is_a_404_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same GAP as the inspect reads: true 404 status, coarse frozen code —
    # FE clients branch on the STATUS (FE3 precedent), so the status is the
    # load-bearing half.
    _project(monkeypatch)
    _active(monkeypatch)

    class _EmptyRepo:
        @classmethod
        def bound_to(cls, conn: Any, binding: Any) -> Any:
            return cls()

        async def fetch_all(self, table: Any, *where: Any) -> Sequence[Any]:
            return []

    _stub(monkeypatch, "BuildScopedRepo", _EmptyRepo)
    r = _preview(client, {"document_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_document_without_raw_fails_loud(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # documents.raw is nullable. Chunking nothing would answer an empty chunk
    # list that reads as "these parameters produce no chunks" — a wrong
    # conclusion about the PARAMETERS when the problem is the DOCUMENT.
    _project(monkeypatch)
    _active(monkeypatch, raw=None)
    r = _preview(client, {"document_id": str(_DOC)})
    assert r.status_code == 400
    assert "no raw text" in r.json()["error"]["message"]


# ---- purity ------------------------------------------------------------------


def test_preview_never_touches_a_write_api(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The endpoint's defining promise (DR-009): nothing is persisted. The repo
    # the router binds is read-only by construction — this pin fails if the
    # handler ever acquires a writer or calls anything beyond fetch_all.
    _project(monkeypatch)

    calls: list[str] = []

    async def fake_resolve(conn: Any, project: str) -> Any:
        return SimpleNamespace(project=project, build_id=_BUILD)

    class _RecordingRepo:
        @classmethod
        def bound_to(cls, conn: Any, binding: Any) -> Any:
            return cls()

        def __getattr__(self, name: str) -> Any:
            async def method(*args: Any, **kwargs: Any) -> Sequence[Any]:
                calls.append(name)
                if name == "fetch_all":
                    return [SimpleNamespace(id=_DOC, raw="some raw text")]
                raise AssertionError(f"preview called a non-read repo method: {name}")

            return method

    _stub(monkeypatch, "_resolve_active_binding", fake_resolve)
    _stub(monkeypatch, "BuildScopedRepo", _RecordingRepo)
    r = _preview(client, {"document_id": str(_DOC), "max_chars": 5, "overlap": 1})
    assert r.status_code == 200
    assert calls == ["fetch_all"]
