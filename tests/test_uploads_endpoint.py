"""Why: the upload endpoint is the CAPTURE end of the DR-010 metadata pipeline,
and its contract guarantees are all "never silently…" ones that only hold if
they are structurally enforced here. These tests pin: a non-multipart body is
415 and an over-limit body is 413 (whole-request refusals, not per-file); a file
with a bad extension or over the single-file limit is a STATED rejected manifest
row with a reason (never a silent drop); the metadata namespace is server-owned
(a ``system`` key in client input is rejected, DR-010 rule 1) and project-typed
(an undeclared/mistyped attribute rejects that file, not the batch); the
submitted filename is the correlation key (duplicates and orphan metadata keys
are 400); and an accepted file's stored envelope carries the server-stamped
``system`` plus the client's validated ``context`` — the exact object handed to
``upsert_managed_source`` for the build to ingest.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import db_conn

pytestmark = pytest.mark.contract

_URL = "/projects/demo/uploads"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[db_conn] = _conn
    with TestClient(app) as c:
        yield c


def _project(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any] | None = None) -> None:
    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name, config=config or {})

    monkeypatch.setattr("api.routers.uploads.get_project", fake_get_project)


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **overrides: Any) -> None:
    base = {
        "upload_corpus_dir": str(tmp_path),
        "upload_max_total_bytes": 50_000_000,
        "upload_max_file_bytes": 10_000_000,
    }
    base.update(overrides)
    monkeypatch.setattr("api.routers.uploads.get_settings", lambda: SimpleNamespace(**base))


def _capture_source(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub upsert_managed_source, capturing the files map it was handed."""
    captured: dict[str, Any] = {}

    async def fake_upsert(conn: Any, project: str, *, uri: str, kind: str, files: Any) -> Any:
        captured["uri"] = uri
        captured["kind"] = kind
        captured["files"] = files
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr("api.routers.uploads.upsert_managed_source", fake_upsert)
    return captured


# --- whole-request refusals --------------------------------------------------


def test_non_multipart_body_is_415(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    resp = client.post(_URL, json={"files": []})
    assert resp.status_code == 415
    # the whole-request refusal is a raised HTTPException, but it must wear the
    # frozen Error envelope (code/message/request_id), never Starlette's raw
    # {"detail": …} — a client dispatching on error.code must still work
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "multipart" in error["message"] and error["request_id"]


def test_oversized_total_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path, upload_max_total_bytes=5)
    resp = client.post(_URL, files=[("files", ("a.txt", b"hello world", "text/plain"))])
    assert resp.status_code == 413
    # same: the 413 also wears the frozen envelope, not {"detail": …}
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "limit" in error["message"] and error["request_id"]


def test_no_files_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    # a multipart body whose file part is under the wrong field name — no 'files'
    resp = client.post(_URL, files=[("notfiles", ("x.txt", b"y", "text/plain"))])
    assert resp.status_code == 400
    assert "at least one file part" in resp.json()["error"]["message"]


def test_duplicate_filenames_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    resp = client.post(
        _URL,
        files=[
            ("files", ("dup.txt", b"one", "text/plain")),
            ("files", ("dup.txt", b"two", "text/plain")),
        ],
    )
    assert resp.status_code == 400
    assert "duplicate" in resp.json()["error"]["message"]


# --- per-file outcomes (stated, never silent) --------------------------------


def test_accepted_and_rejected_files_manifest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    captured = _capture_source(monkeypatch)
    resp = client.post(
        _URL,
        files=[
            ("files", ("good.txt", b"hello", "text/plain")),
            ("files", ("bad.exe", b"MZ...", "application/octet-stream")),
        ],
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["source_id"] is not None
    by_original = {f["original_filename"]: f for f in data["files"]}
    good, bad = by_original["good.txt"], by_original["bad.exe"]
    assert good["status"] == "accepted"
    assert good["filename"].endswith(".txt") and good["filename"] != "good.txt"
    assert good["document_uri"].startswith("file://")
    assert good["metadata"]["system"] == {"connector": "upload", "original_filename": "good.txt"}
    assert "reason" not in good
    assert bad["status"] == "rejected"
    assert "not allowlisted" in bad["reason"]
    assert "filename" not in bad and "document_uri" not in bad
    # the accepted file (and only it) was stashed on the managed source, keyed by
    # its STORED name, with the full envelope for the build to ingest
    assert set(captured["files"]) == {good["filename"]}
    assert captured["files"][good["filename"]]["system"]["original_filename"] == "good.txt"
    # the accepted file's bytes were actually written to the managed corpus
    assert (tmp_path / "demo" / good["filename"]).read_bytes() == b"hello"


def test_oversized_single_file_is_rejected_not_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path, upload_max_file_bytes=3)
    _capture_source(monkeypatch)
    resp = client.post(_URL, files=[("files", ("big.txt", b"way too long", "text/plain"))])
    assert resp.status_code == 201  # a per-file reject, NOT a whole-request failure
    row = resp.json()["data"]["files"][0]
    assert row["status"] == "rejected" and "single-file limit" in row["reason"]
    assert resp.json()["data"]["source_id"] is None  # nothing accepted → no source


# --- metadata (server-owned system, project-typed attributes) ----------------


def test_system_injection_in_metadata_rejects_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    metadata = {"doc.txt": {"system": {"connector": "forged"}}}
    resp = client.post(
        _URL,
        files=[("files", ("doc.txt", b"x", "text/plain"))],
        data={"metadata": json.dumps(metadata)},
    )
    assert resp.status_code == 201
    row = resp.json()["data"]["files"][0]
    assert row["status"] == "rejected" and "metadata is invalid" in row["reason"]


def test_undeclared_attribute_rejects_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch, config={"metadata_schema": {"attributes": {}}})
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    metadata = {"doc.txt": {"context": {"attributes": {"case_number": "42"}}}}
    resp = client.post(
        _URL,
        files=[("files", ("doc.txt", b"x", "text/plain"))],
        data={"metadata": json.dumps(metadata)},
    )
    assert resp.status_code == 201
    row = resp.json()["data"]["files"][0]
    assert row["status"] == "rejected" and "does not match the project schema" in row["reason"]


def test_orphan_metadata_key_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    metadata = {"ghost.txt": {"context": {"title": "x"}}}
    resp = client.post(
        _URL,
        files=[("files", ("real.txt", b"x", "text/plain"))],
        data={"metadata": json.dumps(metadata)},
    )
    assert resp.status_code == 400
    assert "ghost.txt" in resp.json()["error"]["message"]


def test_null_per_file_metadata_entry_rejects_that_file_not_the_batch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: a metadata map value of explicit null ({"nulled.txt": null}) is PRESENT-but-
    # null, NOT absent — the contract types each value as a DocumentMetadataInput object.
    # It must be a STATED per-file rejection, never silently rewritten to an empty
    # server-stamped envelope (the same null strictness DocumentMetadataInput enforces on
    # its context/governance sub-objects). A file with NO entry at all stays accepted —
    # absent ≠ null is exactly the distinction, so a truthiness/`.get()` collapse regresses.
    _project(monkeypatch)  # empty schema: an absent entry is accepted, isolating the null case
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    resp = client.post(
        _URL,
        files=[
            ("files", ("nulled.txt", b"x", "text/plain")),
            ("files", ("absent.txt", b"y", "text/plain")),  # no metadata entry at all
        ],
        data={"metadata": json.dumps({"nulled.txt": None})},
    )
    assert resp.status_code == 201  # per-file reject, not a whole-request failure
    rows = {r["original_filename"]: r for r in resp.json()["data"]["files"]}
    assert rows["nulled.txt"]["status"] == "rejected"
    assert "metadata is invalid" in rows["nulled.txt"]["reason"]
    assert rows["absent.txt"]["status"] == "accepted"  # absent stays accepted


def test_non_finite_metadata_constant_is_400_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: json.loads accepts the non-standard constants NaN/Infinity/-Infinity by
    # default; a non-finite float would pass a `number` attribute or the open governance
    # bag and then 500 downstream (Postgres JSONB refuses non-finite). A malformed upload
    # must be the documented 400 at parse time, never a 500 — parse_constant rejects it.
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    for raw in (
        '{"doc.txt": {"context": {"attributes": {"year": NaN}}}}',
        '{"doc.txt": {"governance": {"score": Infinity}}}',
    ):
        resp = client.post(
            _URL,
            files=[("files", ("doc.txt", b"x", "text/plain"))],
            data={"metadata": raw},
        )
        assert resp.status_code == 400, raw
        error = resp.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert "non-finite" in error["message"]


def test_valid_metadata_is_stamped_into_the_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(
        monkeypatch,
        config={"metadata_schema": {"attributes": {"case_number": {"type": "string"}}}},
    )
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    metadata = {
        "ruling.txt": {
            "context": {"title": "Ruling 42", "attributes": {"case_number": "42"}},
            "governance": {"visibility": "restricted"},
        }
    }
    resp = client.post(
        _URL,
        files=[("files", ("ruling.txt", b"body", "text/plain"))],
        data={"metadata": json.dumps(metadata)},
    )
    assert resp.status_code == 201
    envelope = resp.json()["data"]["files"][0]["metadata"]
    assert envelope["context"]["title"] == "Ruling 42"
    assert envelope["context"]["attributes"] == {"case_number": "42"}
    # exclude_none: only the supplied governance field is stored (no classification:null)
    assert envelope["governance"] == {"visibility": "restricted"}
    assert envelope["system"]["connector"] == "upload"
    assert envelope["schema_version"] == "1.0"


def test_missing_required_attribute_rejects_file_with_no_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: a project's REQUIRED attribute must be enforced even when the client
    # supplies NO metadata for a file — else the schema is non-load-bearing for
    # the common "no metadata" case and silently stores a document later stages
    # assume was rejected (a class-23 write-side silent brick).
    _project(
        monkeypatch,
        config={
            "metadata_schema": {"attributes": {"case_number": {"type": "string", "required": True}}}
        },
    )
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    resp = client.post(_URL, files=[("files", ("req.txt", b"body", "text/plain"))])
    assert resp.status_code == 201  # a per-file STATED reject, not a whole-request failure
    row = resp.json()["data"]["files"][0]
    assert row["status"] == "rejected"
    assert "required attribute 'case_number'" in row["reason"]
    assert resp.json()["data"]["source_id"] is None  # nothing accepted → no source


def test_missing_required_attribute_rejects_file_with_metadata_but_no_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # the same gap on the metadata-present-but-no-context path: governance only,
    # no context → the required attribute is still missing and must reject.
    _project(
        monkeypatch,
        config={
            "metadata_schema": {"attributes": {"case_number": {"type": "string", "required": True}}}
        },
    )
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    metadata = {"doc.txt": {"governance": {"visibility": "public"}}}
    resp = client.post(
        _URL,
        files=[("files", ("doc.txt", b"x", "text/plain"))],
        data={"metadata": json.dumps(metadata)},
    )
    assert resp.status_code == 201
    row = resp.json()["data"]["files"][0]
    assert row["status"] == "rejected" and "required attribute 'case_number'" in row["reason"]


def test_malformed_metadata_schema_is_400_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: a malformed projects.config.metadata_schema (e.g. an unsupported
    # attribute type left by PATCH /projects) is a config VALIDATION problem — the
    # endpoint must return a typed 400, same as the query router does for a bad
    # exposure block, never a 500 INTERNAL that hides an operator-fixable error.
    _project(monkeypatch, config={"metadata_schema": {"attributes": {"x": {"type": "bogus"}}}})
    _settings(monkeypatch, tmp_path)
    resp = client.post(_URL, files=[("files", ("a.txt", b"hi", "text/plain"))])
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"metadata_schema": "invalid"}


def test_project_name_escaping_corpus_root_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: the project name is a managed-corpus PATH COMPONENT; a name like '..'
    # would let the corpus escape upload_corpus_dir — writing generated files
    # under the parent dir and registering that escaped dir as the canonical
    # source, so a later build could ingest unrelated local files. Must be a 400,
    # BEFORE any filesystem I/O.
    _project(monkeypatch)  # get_project stub returns a project for any name
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    resp = client.post(
        "/projects/%2E%2E/uploads", files=[("files", ("a.txt", b"hi", "text/plain"))]
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "path component" in error["message"]
    # nothing escaped: no file was written outside the corpus root
    assert not list(tmp_path.parent.glob("*.txt"))


def test_null_metadata_subobject_rejects_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: the frozen contract makes context/governance OPTIONAL properties, not
    # nullable. An explicit null (`{"context": null}`) must be a VALIDATION_ERROR,
    # not silently accepted and stored as an empty envelope (contract drift).
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    metadata = {"doc.txt": {"context": None}}
    resp = client.post(
        _URL,
        files=[("files", ("doc.txt", b"x", "text/plain"))],
        data={"metadata": json.dumps(metadata)},
    )
    assert resp.status_code == 201  # per-file STATED reject (same as other bad metadata)
    row = resp.json()["data"]["files"][0]
    assert row["status"] == "rejected"
    assert "metadata is invalid" in row["reason"]
    assert "must be an object when present" in row["reason"]


def test_failed_source_registration_writes_no_corpus_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: bytes must reach the scanned corpus only AFTER the managed-source
    # registration is in the transaction. If upsert fails (e.g. a concurrent
    # project delete raises ProjectNotFoundError), NO file may be left on disk —
    # else a later build would ingest that orphan with fallback metadata.
    from core.registry import ProjectNotFoundError

    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)

    async def failing_upsert(conn: Any, project: str, *, uri: str, kind: str, files: Any) -> Any:
        raise ProjectNotFoundError(project)

    monkeypatch.setattr("api.routers.uploads.upsert_managed_source", failing_upsert)

    resp = client.post(_URL, files=[("files", ("a.txt", b"hi", "text/plain"))])
    assert resp.status_code == 404  # ProjectNotFoundError → PROJECT_NOT_FOUND
    # the corpus dir may exist (created for the manifest URI) but holds NO file:
    # the write is strictly after the (failed) registration
    corpus = tmp_path / "demo"
    assert not (corpus.exists() and list(corpus.glob("*.txt")))


def test_null_context_attributes_rejects_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: the contract makes context.attributes an OBJECT when present, not
    # nullable. `{"context": {"attributes": null}}` must reject, not be normalized
    # to an empty object — the same non-nullable rule as the context/governance
    # sub-objects, one level deeper.
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    metadata = {"doc.txt": {"context": {"attributes": None}}}
    resp = client.post(
        _URL,
        files=[("files", ("doc.txt", b"x", "text/plain"))],
        data={"metadata": json.dumps(metadata)},
    )
    assert resp.status_code == 201
    row = resp.json()["data"]["files"][0]
    assert row["status"] == "rejected"
    assert "attributes must be an object when present" in row["reason"]
