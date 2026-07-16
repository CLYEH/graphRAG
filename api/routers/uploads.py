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

import hashlib
import json
import math
import uuid
from pathlib import Path
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from api.deps import ConnProvider, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.idempotency import request_hash, run_idempotent
from api.registry_errors import translate_registry_error
from api.schemas import DocumentMetadataInput
from core.builds.sources import SourceResolutionError, ensure_resolvable_file_uri
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

#: Sentinel distinguishing a filename ABSENT from the metadata map (the file carried
#: no per-file metadata → empty server-stamped envelope) from a filename PRESENT with
#: an explicit ``null`` (or any non-object) value. The contract types each metadata
#: value as a ``DocumentMetadataInput`` object, so a present-but-null entry is a STATED
#: per-file rejection — never silently rewritten to an empty envelope (the same null
#: strictness DocumentMetadataInput enforces on its context/governance sub-objects).
_METADATA_ABSENT: Any = object()


@router.post("/projects/{project}/uploads")
async def upload_documents_endpoint(
    request: Request,
    open_conn: ConnProvider,
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

    # Path-safety BEFORE any filesystem I/O (a 400, fail-closed): a project name
    # like '..' or one with separators would let the corpus escape the root.
    # (Depends only on the name — deterministic per request, so it is safe to run
    # before the idempotent replay; project existence/config are checked inside
    # produce, see below.)
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
    # The contract types `files` as an array of BINARY parts. Starlette parses a part
    # named `files` that carries no filename (a text form field) as a str, not an
    # UploadFile — filtering those away would SILENTLY drop a submitted part (a client
    # that mis-encodes one file as a form field loses it whenever another valid file is
    # present). So reject any non-file `files` part as a stated whole-request 400 rather
    # than dropping it, the same no-silent-drop capture guarantee the metadata field keeps.
    file_parts = form.getlist("files")
    files = [part for part in file_parts if isinstance(part, UploadFile)]
    if len(files) != len(file_parts):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "every 'files' part must be an uploaded file (a binary part with a "
            "filename), not a text form field — a non-file 'files' part would be "
            "silently dropped",
            details={"non_file_files_parts": len(file_parts) - len(files)},
        )
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
    # A NUL in a filename is the SAME U+0000→JSONB-500 class the metadata guard closes, on
    # a second string feeding the same write: the submitted filename is echoed verbatim into
    # the stored envelope's server-owned system.original_filename, and Postgres text/JSONB
    # cannot hold U+0000 — so an accepted NUL filename 500s the upsert_managed_source write.
    # Reject it as a whole-request 400 here (a filename with a NUL is not a valid name on any
    # filesystem, and it is the correlation key), grouped with the empty/duplicate filename
    # guards so it also covers a file that carries no per-file metadata (the per-file
    # _contains_nul guard only scans a file's supplied context/governance).
    if any("\x00" in name for name in submitted_names):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "a submitted filename contains a NUL character (U+0000), which is not a valid "
            "filename and cannot be stored",
        )
    duplicates = sorted({n for n in submitted_names if submitted_names.count(n) > 1})
    if duplicates:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"duplicate submitted filename(s) {duplicates} — each filename must be unique "
            "within a request (it keys both the managed corpus identity and the metadata)",
            details={"duplicate_filenames": duplicates},
        )

    # `metadata` is a SINGLE JSON object (contract: object, not array), unlike the
    # repeated `files` parts. form.get() would silently keep just one of several
    # `metadata` parts and drop the rest, so a file's supplied envelope could vanish
    # while the file is still accepted — exactly the no-silent-drop violation this
    # endpoint refuses. getlist surfaces the count so >1 is a stated whole-request 400.
    metadata_parts = form.getlist("metadata")
    if len(metadata_parts) > 1:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"the 'metadata' field must appear at most once, but {len(metadata_parts)} "
            "parts were sent — it is a single JSON object keyed by submitted filename, "
            "so repeated parts would silently drop all but one",
            details={"metadata_part_count": len(metadata_parts)},
        )
    raw_metadata = metadata_parts[0] if metadata_parts else None
    metadata_by_name = _parse_metadata_field(raw_metadata, set(submitted_names))

    async def produce() -> tuple[int, dict[str, Any]]:
        # Project existence + config are read INSIDE the idempotent region so a
        # retry with the same Idempotency-Key after the project was deleted (404)
        # or its metadata_schema changed (400) REPLAYS the stored response rather
        # than surfacing a fresh error — run_idempotent's reserve-fail path skips
        # produce entirely, and the idempotency row isn't project-FK-scoped.
        project_row = await get_project(conn, project)
        if project_row is None:
            raise ApiError(
                ErrorCode.PROJECT_NOT_FOUND,
                f"project {project!r} not found",
                details={"project": project},
            )
        try:
            schema = load_metadata_schema(project_row.config)
        except MetadataConfigError as exc:
            # a malformed project metadata_schema is a config validation problem →
            # a typed 400 (same strictness as the query router's exposure loading),
            # never a 500 INTERNAL that hides an operator-fixable config error
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, str(exc), details={"metadata_schema": "invalid"}
            ) from exc
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
            # Write the bytes only AFTER the source registration is in the txn, so a
            # failed upsert (e.g. a concurrent project delete) leaves no orphan in
            # the scanned corpus. A file orphaned by a later COMMIT failure is still
            # never ingested — read_text_documents only reads REGISTERED files.
            _write_corpus_files(corpus_dir, accepted)
            source_id = str(source.id)
        result = {"source_id": source_id, "files": manifest}
        return 201, success(result, **response_meta(request))

    # DB checkout is deferred to HERE — after every header/body-shape gate (415,
    # 413, path-safety) and the full multipart parse — so a request refused by those
    # gates, or one still streaming a large body, never holds a pool connection (and
    # a DB outage can't turn a header-only refusal into a 500). `produce` closes over
    # `conn`, which is bound by this block before `produce()` is ever called.
    async with open_conn() as conn:
        if idempotency_key:
            status, resp = await run_idempotent(
                conn,
                key=idempotency_key,
                project=project,
                endpoint="uploadDocuments",
                # Hash the CANONICAL request (submitted names + file bytes + parsed
                # metadata), NOT the raw multipart body: encoders pick a fresh random
                # boundary each time, so a faithful retry of the same files/metadata under
                # the same Idempotency-Key would otherwise hash differently and 409
                # IDEMPOTENCY_CONFLICT instead of replaying the first 201.
                req_hash=request_hash(
                    "POST",
                    request.url.path,
                    await _canonical_upload_fingerprint(files, metadata_by_name),
                ),
                produce=produce,
            )
            return JSONResponse(status_code=status, content=resp)
        status, resp = await produce()
        return JSONResponse(status_code=status, content=jsonable_encoder(resp))


def _reject_unsafe_corpus_path(settings: Any, project: str) -> None:
    """Raise a 400 if the project name can't back a resolvable managed corpus.

    The project name is a path component of ``upload_corpus_dir`` (``_corpus_dir``),
    but ``ProjectCreate`` only checks ``min_length``. Two failure modes, both a 400
    BEFORE any file I/O:

    * a name like ``..`` or one with separators would let the corpus escape the root,
      writing generated files outside it AND registering that escaped dir as the
      canonical source (a later build could then ingest unrelated local files) —
      delegated to the shared ``safe_project_subdir`` (the guard the eval worker uses);
    * a name that IS a safe path component but whose corpus ``as_uri()`` encodes to a
      form the source resolver rejects (``foo:bar`` → ``%3A``, ``foo|bar`` → ``|``) —
      the upload would register a managed source EVERY later build then fails to
      resolve. ``ensure_resolvable_file_uri`` applies the exact source-resolution rules
      so the name is refused at capture, not accepted into an unbuildable source.

    Kept SYNC (like ``_corpus_dir``) so the filesystem-touching ``resolve()`` stays off
    the async endpoint's blocking-call lint, and called BEFORE any file I/O."""
    corpus_dir = safe_project_subdir(Path(settings.upload_corpus_dir), project)
    if corpus_dir is None:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"project {project!r} is not a valid managed-corpus path component",
            details={"project": project},
        )
    try:
        ensure_resolvable_file_uri(corpus_dir.as_uri())
    except SourceResolutionError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"project {project!r} produces a managed-corpus URI that builds cannot "
            "resolve — avoid characters like ':' or '|' in the project name",
            details={"project": project},
        ) from exc


def _corpus_dir(settings: Any, project: str) -> Path:
    """The project's managed corpus directory (absolute), created on demand. The
    same path across uploads so the managed source is ONE per project. The
    endpoint has already rejected an unsafe project name
    (``_reject_unsafe_corpus_path``), so the containment re-check here can't fail."""
    root = safe_project_subdir(Path(settings.upload_corpus_dir), project)
    assert root is not None  # guarded by _reject_unsafe_corpus_path before any I/O
    root.mkdir(parents=True, exist_ok=True)
    return root


def _reject_non_finite_constant(value: str) -> NoReturn:
    """``json.loads(parse_constant=…)`` hook: fired only for ``NaN``/``Infinity``/
    ``-Infinity``. Raising here rejects them at parse time (the caller maps it to a
    400) instead of letting a non-finite float reach Postgres JSONB and 500."""
    raise ValueError(f"non-finite constant {value!r} is not allowed")


def _finite_float(value: str) -> float:
    """``json.loads(parse_float=…)`` hook for every float token. Rejects one that
    OVERFLOWS to a non-finite float (``1e999`` → ``inf``) — a token parse_constant
    never sees — so it cannot pass a ``number`` attribute and 500 in Postgres JSONB."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite number {value!r} is not allowed")
    return parsed


def _contains_nul(obj: Any) -> bool:
    """True if any string key or value nested in a parsed-metadata structure holds a
    NUL (U+0000). A JSON string may legally carry ``\\u0000``, but Postgres text/JSONB
    cannot store it — the same JSON-valid-but-JSONB-unstorable class as the non-finite
    guards above — so such a value would 500 the ``upsert_managed_source`` write. Scans
    keys too: the open ``context.attributes`` / ``governance`` bags let a client NUL hide
    in an object KEY, which Postgres rejects just as it does a value."""
    if isinstance(obj, str):
        return "\x00" in obj
    if isinstance(obj, dict):
        return any((isinstance(k, str) and "\x00" in k) or _contains_nul(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_contains_nul(v) for v in obj)
    return False


async def _canonical_upload_fingerprint(
    files: list[UploadFile], metadata_by_name: dict[str, Any]
) -> bytes:
    """A boundary-independent idempotency fingerprint for an upload: the submitted
    filenames, each file's BYTES, and the parsed metadata — NOT the raw multipart
    framing (whose boundary a client's encoder re-randomizes on every retry). Files
    are folded in submitted-NAME order (they are already duplicate-rejected, so the
    name set is unique) so a retry that reorders the parts still matches; each is
    ``seek(0)``'d back afterward so ``_process_files`` re-reads it from the start.
    Length is folded before content so no name/content boundary is ambiguous, and the
    parsed metadata is canonical JSON (sorted keys) so key order can't spuriously
    differ. Returned digest is handed to ``request_hash`` (which folds method+path)."""
    hasher = hashlib.sha256()
    for part in sorted(files, key=lambda p: p.filename or ""):
        name = part.filename or ""
        content = await part.read()
        await part.seek(0)
        hasher.update(name.encode())
        hasher.update(b"\0")
        hasher.update(str(len(content)).encode())
        hasher.update(b"\0")
        hasher.update(content)
        hasher.update(b"\0")
    hasher.update(json.dumps(metadata_by_name, sort_keys=True, separators=(",", ":")).encode())
    return hasher.digest()


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
        # Two hooks close the non-finite→JSONB-500 path (RFC 8259 JSON has no non-finite
        # numbers): parse_constant catches the literal NaN/Infinity/-Infinity tokens
        # json.loads accepts by default; parse_float catches a VALID number token that
        # OVERFLOWS to inf (e.g. `1e999` → float('inf')), which never reaches
        # parse_constant. Either would otherwise pass a `number` attribute or the open
        # governance bag and 500 in Postgres JSONB — a malformed upload must be the
        # documented 400, not a 500. (Huge INTEGERS stay Python int → JSONB-safe.)
        # JSONDecodeError is a ValueError, so one except covers malformed JSON + both.
        parsed = json.loads(
            raw, parse_constant=_reject_non_finite_constant, parse_float=_finite_float
        )
    except ValueError as exc:
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
    (stored_name, envelope, content) — the CONTENT is held in memory and NOT
    written here; the caller writes it only AFTER the managed-source registration
    is in the transaction (``_write_corpus_files``), so a failed registration
    leaves no file in the scanned corpus."""
    manifest: list[dict[str, Any]] = []
    accepted: list[tuple[str, dict[str, Any], bytes]] = []
    for part in files:
        original = part.filename or ""
        content = await part.read()
        reason, parsed = _validate_file(
            original, content, metadata_by_name.get(original, _METADATA_ABSENT), schema, settings
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
    return manifest, accepted


def _write_corpus_files(
    corpus_dir: Path, accepted: list[tuple[str, dict[str, Any], bytes]]
) -> None:
    """Write each accepted file's bytes into the managed corpus under its stored
    name. Called only AFTER ``upsert_managed_source`` has registered them in the
    transaction, so an upsert failure (e.g. a concurrent project delete) never
    leaves an orphan in the scanned corpus. Kept SYNC (like ``_corpus_dir``) so
    the pathlib writes stay off the async endpoint's blocking-call lint."""
    for stored_name, _envelope, content in accepted:
        (corpus_dir / stored_name).write_bytes(content)


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
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        # The managed text connector reads every accepted file with
        # read_text(encoding="utf-8"), so a non-UTF-8 .txt/.md would raise and fail
        # EVERY build over this upload at ingest. Reject it HERE as a STATED per-file
        # manifest row (at capture), not a build-time crash — the allowlisted suffixes
        # are all UTF-8 text formats, so undecodable bytes are a per-file refusal.
        return f"file is not valid UTF-8 text (cannot decode at byte {exc.start})", None
    parsed: DocumentMetadataInput | None = None
    context_to_check: dict[str, Any] = {}
    # A filename PRESENT in the metadata map (even as an explicit null) is validated;
    # only a truly ABSENT one (_METADATA_ABSENT) skips to the empty-context path. So
    # `{"doc.txt": null}` / a non-object entry hits model_validate below and is a
    # STATED per-file rejection, not a silent empty envelope (contract: each value is
    # a DocumentMetadataInput object).
    if entry is not _METADATA_ABSENT:
        try:
            parsed = DocumentMetadataInput.model_validate(entry)
        except ValidationError as exc:
            return f"metadata is invalid: {_first_pydantic_error(exc)}", None
        if _contains_nul(parsed.model_dump()):
            # A JSON string may hold U+0000, but Postgres text/JSONB cannot store it, so
            # upsert_managed_source would 500 the WHOLE upload rather than this file's
            # stated metadata refusal. Reject at capture (same class as the non-finite
            # guards in _parse_metadata_field), before the accept path reaches the DB.
            # Per-file here (a file's own metadata is its own concern), unlike the
            # non-finite guard (a parse-time hook that can't attribute the token to a
            # file → whole-request 400) and the NUL-filename guard (a structural
            # correlation-key check → whole-request 400): granularity follows where the
            # bad string lives, not the shared JSONB-unstorable class.
            return "metadata contains a NUL character (U+0000), which cannot be stored", None
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
