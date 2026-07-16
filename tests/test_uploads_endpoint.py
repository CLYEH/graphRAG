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

import io
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from api.app import create_app
from api.deps import db_conn_provider
from api.routers.uploads import _canonical_upload_fingerprint

pytestmark = pytest.mark.contract

_URL = "/projects/demo/uploads"


def _fake_conn_provider(
    on_open: Callable[[], None] | None = None,
) -> Callable[[], Callable[[], AbstractAsyncContextManager[object]]]:
    """A db_conn_provider override: a lazy handle whose opened context manager yields
    a throwaway conn (the DB calls are stubbed). ``on_open`` fires when the connection
    is actually opened, so a test can assert the checkout is deferred past preflight."""

    @asynccontextmanager
    async def _open() -> AsyncIterator[object]:
        if on_open is not None:
            on_open()
        yield object()

    def _provider() -> Callable[[], AbstractAsyncContextManager[object]]:
        return _open

    return _provider


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[db_conn_provider] = _fake_conn_provider()
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


def test_project_name_with_unresolvable_corpus_uri_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY (triage 33): the project name is a path component of the managed corpus, and the
    # corpus's `as_uri()` is registered as the source every build resolves. A name that is a
    # SAFE path component but whose as_uri() encodes to a form the source resolver rejects
    # (a pipe '|' → the Windows drive separator; a colon → %3A) would let the upload succeed
    # and register a source EVERY later build then fails to resolve — an unbuildable upload.
    # It must be a STATED whole-request 400 at capture (the same source-resolution rules),
    # never accepted. Revert-probe: drop the ensure_resolvable_file_uri check and this is no
    # longer a 400 at capture (it slips past the corpus guard into the accept path).
    _settings(monkeypatch, tmp_path)
    # '|' is percent-encoded to %7C by as_uri(), decodes back to '|', which the resolver
    # refuses (the drive separator); the URL carries it as %7C so the path param is 'a|b'.
    resp = client.post(
        "/projects/a%7Cb/uploads",
        files=[("files", ("doc.txt", b"hello", "text/plain"))],
    )
    assert resp.status_code == 400  # whole-request refusal, before any file work
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "resolve" in error["message"]  # the corpus URI builds cannot resolve


def test_db_checkout_is_deferred_until_after_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY (triage 30): the DB connection is a LAZY handle (db_conn_provider), opened
    # only INSIDE the handler after the header/body-shape gates — so a request refused
    # by those gates never checks out a pool slot (and a DB outage can't turn a
    # header-only 415 into a 500, nor can slow large-body streams exhaust the pool
    # while doing no DB work). Assert the connection opens EXACTLY on the DB-work path:
    # never for a 415, and exactly once for an accepted upload. Revert-probe: widen the
    # `async with open_conn()` to wrap the preflight (or restore an eager `conn: Conn`)
    # and the 415 opens a connection too, flipping `opened` to non-zero.
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    opened = {"n": 0}
    app = create_app()
    app.dependency_overrides[db_conn_provider] = _fake_conn_provider(
        on_open=lambda: opened.__setitem__("n", opened["n"] + 1)
    )
    with TestClient(app) as c:
        # a 415 (non-multipart) is refused from the content-type header alone
        refused = c.post(_URL, json={"files": []})
        assert refused.status_code == 415
        assert opened["n"] == 0  # the DB was never touched for a header-only refusal
        # an accepted upload DOES open the connection — exactly once, on the DB-work path
        ok = c.post(_URL, files=[("files", ("a.txt", b"hello", "text/plain"))])
        assert ok.status_code == 201
        assert opened["n"] == 1


def test_no_files_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    # a multipart body whose file part is under the wrong field name — no 'files'
    resp = client.post(_URL, files=[("notfiles", ("x.txt", b"y", "text/plain"))])
    assert resp.status_code == 400
    assert "at least one file part" in resp.json()["error"]["message"]


def test_non_file_files_part_is_400_not_silently_dropped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: the contract types `files` as an array of BINARY parts. A part named `files`
    # with no filename is parsed as a text field (str), not an UploadFile. Filtering it
    # away would SILENTLY drop a submitted part whenever a valid file is also present —
    # a client that mis-encodes one file as a form field loses it with no manifest row.
    # It must be a STATED whole-request 400, never a silent one-of-N drop (the same
    # no-silent-drop guarantee the endpoint keeps for every submitted part).
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    # httpx `data=` emits a second part named `files` as a text field (no filename),
    # alongside the real binary file part — exactly the mis-encoded-file shape.
    resp = client.post(
        _URL,
        files=[("files", ("real.txt", b"x", "text/plain"))],
        data={"files": "oops-encoded-as-text"},
    )
    assert resp.status_code == 400  # whole-request refusal, not a 201 with the text lost
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "must be an uploaded file" in error["message"]
    assert error["details"]["non_file_files_parts"] == 1


async def test_canonical_upload_fingerprint_is_content_based_not_framing() -> None:
    # WHY (triage 24): the idempotency hash keys on the CANONICAL request (submitted
    # names + file bytes + parsed metadata), NOT the multipart framing, so a faithful
    # retry under a fresh boundary replays instead of 409-ing. Pin the properties the
    # endpoint relies on: identical content → identical digest even when the parts are
    # REORDERED, and any file-byte or metadata change → a different digest. Also pin
    # that each part is seek(0)'d back so _process_files re-reads it from the start.
    def _uf(name: str, content: bytes) -> UploadFile:
        return UploadFile(file=io.BytesIO(content), filename=name, size=len(content))

    meta = {"a.txt": {"context": {"title": "A"}}}
    base = await _canonical_upload_fingerprint([_uf("a.txt", b"one"), _uf("b.txt", b"two")], meta)
    reordered = await _canonical_upload_fingerprint(
        [_uf("b.txt", b"two"), _uf("a.txt", b"one")], meta
    )
    assert base == reordered  # order-independent (a retry may reorder the parts)
    diff_bytes = await _canonical_upload_fingerprint(
        [_uf("a.txt", b"ONE"), _uf("b.txt", b"two")], meta
    )
    assert diff_bytes != base  # different file content → different request
    diff_meta = await _canonical_upload_fingerprint(
        [_uf("a.txt", b"one"), _uf("b.txt", b"two")], {"a.txt": {"context": {"title": "B"}}}
    )
    assert diff_meta != base  # different metadata → different request

    f = _uf("a.txt", b"hello")
    await _canonical_upload_fingerprint([f], {})
    assert await f.read() == b"hello"  # seek(0)'d back for _process_files to re-read


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


def test_non_utf8_file_is_rejected_not_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY (triage 28): the managed text connector reads every accepted file with
    # read_text(encoding="utf-8"), so a non-UTF-8 .txt/.md would raise and fail EVERY
    # build over this upload at ingest. It must be a STATED per-file rejection AT
    # CAPTURE (a rejected manifest row), never accepted to crash a later build — and a
    # valid sibling in the same batch stays accepted (per-file, not whole-request).
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    resp = client.post(
        _URL,
        files=[
            ("files", ("bad.txt", b"\xff\xfenot utf-8", "text/plain")),
            ("files", ("good.txt", b"clean text", "text/plain")),
        ],
    )
    assert resp.status_code == 201  # per-file reject, not a whole-request failure
    rows = {r["original_filename"]: r for r in resp.json()["data"]["files"]}
    assert rows["bad.txt"]["status"] == "rejected"
    assert "UTF-8" in rows["bad.txt"]["reason"]
    assert rows["good.txt"]["status"] == "accepted"  # the decodable sibling is unaffected


def test_metadata_with_nul_is_rejected_not_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY (triage 29): a metadata string may legally carry U+0000 (json.loads and the
    # DocumentMetadataInput validation both accept it), but Postgres text/JSONB cannot
    # store U+0000 — so it would 500 the WHOLE upload at the upsert_managed_source write
    # instead of this file's STATED per-file metadata refusal. It must be rejected AT
    # CAPTURE, recursively (here in an open governance value), and a clean sibling in the
    # same batch stays accepted (per-file, not whole-request). Revert-probe: drop the
    # _contains_nul guard and bad.txt is accepted, and the NUL rides into the JSONB write.
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    metadata = {"bad.txt": {"governance": {"note": "secret\x00leak"}}}
    resp = client.post(
        _URL,
        files=[
            ("files", ("bad.txt", b"clean text", "text/plain")),
            ("files", ("good.txt", b"clean text", "text/plain")),
        ],
        data={"metadata": json.dumps(metadata)},
    )
    assert resp.status_code == 201  # per-file reject, not a whole-request failure/500
    rows = {r["original_filename"]: r for r in resp.json()["data"]["files"]}
    assert rows["bad.txt"]["status"] == "rejected"
    assert "NUL" in rows["bad.txt"]["reason"]
    assert rows["good.txt"]["status"] == "accepted"  # the clean sibling is unaffected


def test_nul_in_filename_is_rejected_not_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY (triage 29, filename sibling): the submitted filename is echoed VERBATIM into the
    # stored envelope's server-owned system.original_filename — a JSONB write — so a raw NUL
    # (U+0000) in it is the SAME "accepted then 500s the upsert" class as a NUL in metadata
    # (Postgres text/JSONB cannot store U+0000). It also evades the per-file metadata guard
    # entirely for a file carrying no metadata. So it must be a STATED whole-request 400 at
    # capture, never accepted. httpx `files=` percent-encodes a NUL, so the body is built
    # RAW to put the real byte on the wire the way an adversarial client would. Revert-probe:
    # drop the filename NUL guard and this file is accepted and the NUL rides into the write.
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    _capture_source(monkeypatch)
    boundary = "----graphragnulname"
    body = (
        "\r\n".join(
            [
                f"--{boundary}",
                'Content-Disposition: form-data; name="files"; filename="bad\x00.txt"',
                "Content-Type: text/plain",
                "",
                "clean text",
                f"--{boundary}--",
                "",
            ]
        )
    ).encode()
    resp = client.post(
        _URL,
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 400  # whole-request refusal, before any per-file accept
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "NUL" in error["message"]


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


def test_duplicate_metadata_parts_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # WHY: the contract types `metadata` as a SINGLE object (not an array like `files`),
    # so two `metadata` parts is malformed. form.get() would silently keep one and drop
    # the rest — a file could then be accepted with an empty envelope because its supplied
    # metadata sat in the ignored part, violating the endpoint's no-silent-drop capture
    # guarantee. It must be a STATED whole-request 400, never a silent one-of-N pick.
    # (httpx `data=` can't express a repeated key, so the multipart body is built raw.)
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    boundary = "----graphragdupmeta"
    body = (
        "\r\n".join(
            [
                f"--{boundary}",
                'Content-Disposition: form-data; name="files"; filename="a.txt"',
                "Content-Type: text/plain",
                "",
                "body",
                f"--{boundary}",
                'Content-Disposition: form-data; name="metadata"',
                "",
                json.dumps({"a.txt": {"context": {"title": "one"}}}),
                f"--{boundary}",
                'Content-Disposition: form-data; name="metadata"',
                "",
                json.dumps({"a.txt": {"context": {"title": "two"}}}),
                f"--{boundary}--",
                "",
            ]
        )
    ).encode()
    resp = client.post(
        _URL,
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 400  # whole-request refusal, before any per-file work
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "at most once" in error["message"]
    assert error["details"]["metadata_part_count"] == 2


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
    # WHY: json.loads accepts non-finite values two ways — the literal constants
    # NaN/Infinity/-Infinity (parse_constant), AND a valid number token that OVERFLOWS
    # to inf like `1e999` (parse_float, never seen by parse_constant). Either would pass
    # a `number` attribute or the open governance bag and then 500 downstream (Postgres
    # JSONB refuses non-finite). A malformed upload must be the documented 400 at parse
    # time, never a 500 — so BOTH routes are rejected.
    _project(monkeypatch)
    _settings(monkeypatch, tmp_path)
    for raw in (
        '{"doc.txt": {"context": {"attributes": {"year": NaN}}}}',
        '{"doc.txt": {"governance": {"score": Infinity}}}',
        '{"doc.txt": {"context": {"attributes": {"year": 1e999}}}}',  # overflow → inf
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
