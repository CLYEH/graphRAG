"""Upload endpoint (UXC1b, DR-010) — multipart files into a project's managed corpus.

``POST /projects/{project}/uploads`` writes each accepted file into a
server-managed corpus directory and registers/updates ONE canonical ``file://``
source pointing at it (BA9 uri-normalization stays server-owned — the stored uri
is ``Path.resolve().as_uri()``, never a client path). Client filenames are never
used as paths: each accepted file is stored under a generated name and the
original is kept only as metadata (``system.original_filename``).

Per-file outcomes are STATED, never silent (DR-010): a file whose extension is
not allowlisted, that exceeds the single-file size limit, or whose metadata
violates the project schema is a ``rejected`` manifest row with a reason — it is
not dropped. Whole-request refusals are ``415`` (body not multipart) and ``413``
(total size over the configured limit). Optional per-file metadata
(``context``/``governance`` only — ``system`` is stamped server-side) is validated
against the project's ``metadata_schema`` at capture time and stored as the full
DR-010 envelope on the managed source, keyed by the stored filename, for the
build's ingest stage to thread onto ``documents.metadata``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from api.deps import Conn, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.registry_errors import translate_registry_error
from api.schemas import DocumentMetadataInput
from core.config import get_settings
from core.ingest.connectors import TEXT_SUFFIXES
from core.metadata import MetadataValidationError, build_envelope, load_metadata_schema
from core.metadata.schema import MetadataConfigError, MetadataSchema
from core.paths import safe_project_subdir
from core.registry import ProjectNotFoundError, get_project, upsert_managed_source

router = APIRouter(tags=["sources"])

_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]

#: The connector name stamped into every uploaded document's ``system`` namespace.
_UPLOAD_CONNECTOR = "upload"


@router.post("/projects/{project}/uploads")
async def upload_documents_endpoint(
    request: Request,
    conn: Conn,
    project: str,
    idempotency_key: _IdempotencyKey = None,
) -> JSONResponse:
    settings = get_settings()
    # 415: the whole-request content-type gate, before touching the body. HTTP media
    # types are case-insensitive (RFC 9110 §8.3.1), so match on a lowercased header —
    # a client sending "Multipart/form-data" is still a valid multipart request.
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="uploads require multipart/form-data")

    project_row = await get_project(conn, project)
    if project_row is None:
        raise ApiError(
            ErrorCode.PROJECT_NOT_FOUND,
            f"project {project!r} not found",
            details={"project": project},
        )

    # Path-safety BEFORE any filesystem I/O (a 400, fail-closed): a project name
    # like '..' or one with separators would let the corpus escape the root.
    _reject_unsafe_corpus_path(settings, project)

    # 413: refuse an oversized upload by its declared Content-Length FIRST, so an
    # honest large body is rejected before it is buffered; the post-read length
    # check backstops a missing/short Content-Length (the body is still bounded
    # by whatever the ASGI server accepted).
    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > settings.upload_max_total_bytes
    ):
        raise HTTPException(
            status_code=413,
            detail=f"upload total exceeds the {settings.upload_max_total_bytes}-byte limit",
        )
    body = await request.body()
    if len(body) > settings.upload_max_total_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"upload total exceeds the {settings.upload_max_total_bytes}-byte limit",
        )

    form = await request.form()
    files = [part for part in form.getlist("files") if isinstance(part, UploadFile)]
    if not files:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "uploads require at least one file part named 'files'",
        )
    submitted_names = [part.filename or "" for part in files]
    if any(not name for name in submitted_names):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "every file part must carry a filename (it is the manifest's correlation key)",
        )
    duplicates = sorted({n for n in submitted_names if submitted_names.count(n) > 1})
    if duplicates:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"duplicate submitted filename(s) {duplicates} — each filename must be unique "
            "within a request (it keys both the managed corpus identity and the metadata)",
            details={"duplicate_filenames": duplicates},
        )

    metadata_by_name = _parse_metadata_field(form.get("metadata"), set(submitted_names))
    try:
        schema = load_metadata_schema(project_row.config)
    except MetadataConfigError as exc:
        # a malformed project metadata_schema is a config validation problem → a
        # typed 400 (same strictness as the query router's exposure loading),
        # never a 500 INTERNAL that hides an operator-fixable config error
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, str(exc), details={"metadata_schema": "invalid"}
        ) from exc

    async def produce() -> tuple[int, dict[str, Any]]:
        manifest, accepted = await _process_files(
            files, metadata_by_name, schema, settings, project
        )
        source_id: str | None = None
        if accepted:
            corpus_dir = _corpus_dir(settings, project)
            try:
                source = await upsert_managed_source(
                    conn,
                    project,
                    uri=corpus_dir.as_uri(),
                    kind="text",
                    files={name: env for name, env, _ in accepted},
                )
            except ProjectNotFoundError as exc:
                raise translate_registry_error(exc) from exc
            source_id = str(source.id)
        result = {"source_id": source_id, "files": manifest}
        return 201, success(result, **response_meta(request))

    if idempotency_key:
        status, resp = await run_idempotent(
            conn,
            key=idempotency_key,
            project=project,
            endpoint="uploadDocuments",
            req_hash=request_hash("POST", request.url.path, body),
            produce=produce,
        )
        return JSONResponse(status_code=status, content=resp)
    status, resp = await produce()
    return JSONResponse(status_code=status, content=jsonable_encoder(resp))


def _reject_unsafe_corpus_path(settings: Any, project: str) -> None:
    """Raise a 400 if the project name would escape the managed-corpus root.

    The project name is a path component of ``upload_corpus_dir`` (``_corpus_dir``),
    but ``ProjectCreate`` only checks ``min_length`` — a name like ``..`` or one
    with separators would let the corpus escape the root, writing generated files
    outside it AND registering that escaped dir as the canonical source (a later
    build could then ingest unrelated local files). Delegates the containment rule
    to the shared ``safe_project_subdir`` (the same guard the eval worker uses).
    Kept SYNC (like ``_corpus_dir``) so the filesystem-touching ``resolve()`` stays
    off the async endpoint's blocking-call lint, and called BEFORE any file I/O."""
    if safe_project_subdir(Path(settings.upload_corpus_dir), project) is None:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"project {project!r} is not a valid managed-corpus path component",
            details={"project": project},
        )


def _corpus_dir(settings: Any, project: str) -> Path:
    """The project's managed corpus directory (absolute), created on demand. The
    same path across uploads so the managed source is ONE per project. The
    endpoint has already rejected an unsafe project name
    (``_reject_unsafe_corpus_path``), so the containment re-check here can't fail."""
    root = safe_project_subdir(Path(settings.upload_corpus_dir), project)
    assert root is not None  # guarded by _reject_unsafe_corpus_path before any I/O
    root.mkdir(parents=True, exist_ok=True)
    return root


def _parse_metadata_field(raw: Any, submitted_names: set[str]) -> dict[str, Any]:
    """Parse the optional ``metadata`` form field (a JSON object keyed by
    submitted filename). A key that names no submitted file is a client error
    (400) — silently dropping the client's metadata intent is exactly what the
    no-silent-drop guarantee forbids."""
    if raw is None:
        return {}
    if not isinstance(raw, str):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "the 'metadata' form field must be a JSON string"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, f"the 'metadata' field is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "the 'metadata' field must be a JSON object keyed by submitted filename",
        )
    orphans = sorted(set(parsed) - submitted_names)
    if orphans:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"metadata names file(s) {orphans} not in the upload — each metadata key must "
            "match a submitted filename",
            details={"orphan_metadata_keys": orphans},
        )
    return parsed


async def _process_files(
    files: list[UploadFile],
    metadata_by_name: dict[str, Any],
    schema: MetadataSchema,
    settings: Any,
    project: str,
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any], bytes]]]:
    """Validate every file, returning (manifest, accepted). ``accepted`` carries
    (stored_name, envelope, content) — files are written to disk only after ALL
    are validated, so a rejected file never leaves an orphan."""
    manifest: list[dict[str, Any]] = []
    accepted: list[tuple[str, dict[str, Any], bytes]] = []
    for part in files:
        original = part.filename or ""
        content = await part.read()
        reason, parsed = _validate_file(
            original, content, metadata_by_name.get(original), schema, settings
        )
        if reason is not None:
            manifest.append({"original_filename": original, "status": "rejected", "reason": reason})
            continue
        suffix = Path(original).suffix.lower()
        stored_name = f"{uuid.uuid4().hex}{suffix}"
        envelope = _envelope_for(original, parsed)
        accepted.append((stored_name, envelope, content))
        manifest.append(
            {
                "filename": stored_name,
                "original_filename": original,
                "status": "accepted",
                "document_uri": (_corpus_dir(settings, project) / stored_name).as_uri(),
                "metadata": envelope,
            }
        )
    corpus_dir = _corpus_dir(settings, project) if accepted else None
    for stored_name, _envelope, content in accepted:
        assert corpus_dir is not None
        (corpus_dir / stored_name).write_bytes(content)
    return manifest, accepted


def _validate_file(
    original: str,
    content: bytes,
    entry: Any,
    schema: MetadataSchema,
    settings: Any,
) -> tuple[str | None, DocumentMetadataInput | None]:
    """Validate one file for storage. Returns ``(reason, None)`` when rejected — a
    STATED per-file refusal (extension / single-file size / metadata schema) — or
    ``(None, parsed)`` when accepted, where ``parsed`` is the file's validated
    metadata (or None if it carried none). Metadata is validated exactly ONCE here
    and the parsed model is threaded to ``_envelope_for``, so the accept path never
    re-parses it."""
    suffix = Path(original).suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        allowed = sorted(TEXT_SUFFIXES)
        return f"extension {suffix or '(none)'!r} is not allowlisted (allowed: {allowed})", None
    if len(content) > settings.upload_max_file_bytes:
        return f"file exceeds the {settings.upload_max_file_bytes}-byte single-file limit", None
    parsed: DocumentMetadataInput | None = None
    context_to_check: dict[str, Any] = {}
    if entry is not None:
        try:
            parsed = DocumentMetadataInput.model_validate(entry)
        except ValidationError as exc:
            return f"metadata is invalid: {_first_pydantic_error(exc)}", None
        if parsed.context is not None:
            context_to_check = parsed.context.model_dump()
    # validate the context against the project schema ALWAYS — with an empty
    # context when the file supplied none. Skipping this on the no-metadata (or
    # no-context) path would silently accept a file that OMITS a REQUIRED
    # attribute, making the schema non-load-bearing for the common
    # "no metadata supplied" case (a class-23 write-side silent brick).
    try:
        schema.validate_context(context_to_check)
    except MetadataValidationError as exc:
        return f"metadata does not match the project schema: {exc}", None
    return None, parsed


def _envelope_for(original: str, parsed: DocumentMetadataInput | None) -> dict[str, Any]:
    """Build the stored DR-010 envelope from the already-validated input (or an
    empty input when the file carried no metadata). ``system`` is stamped
    server-side."""
    context: dict[str, Any] | None = None
    governance: dict[str, Any] | None = None
    if parsed is not None:
        # exclude_none: store only the fields the client actually supplied — a
        # declared-but-omitted optional (e.g. governance.classification) must not
        # be materialized as an explicit null the exposure/read path then carries
        context = (
            parsed.context.model_dump(exclude_none=True) if parsed.context is not None else None
        )
        governance = (
            parsed.governance.model_dump(exclude_none=True)
            if parsed.governance is not None
            else None
        )
    return build_envelope(
        connector=_UPLOAD_CONNECTOR,
        original_filename=original,
        context=context,
        governance=governance,
    )


def _first_pydantic_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
    return f"{loc}: {err.get('msg', 'invalid')}"
