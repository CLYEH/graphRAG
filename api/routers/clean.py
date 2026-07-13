"""BA4 清洗抽樣預覽 (DESIGN §10.2, contract v1.1 DR-009): preview chunking as a
pure function over ONE source, persisting nothing.

Two sources, exactly one per request (owner decision 2026-07-13):
``document_id`` chunks that document's raw text read from the ACTIVE build —
so an operator can compare parameter sets against real corpus — while ``text``
chunks the given string directly, which is what makes the preview usable
BEFORE any build exists (ingest and build are one pipeline here, so a fresh
project has no documents to point at).

Parameter resolution follows the build's chain: an omitted max_chars /
overlap falls back to the project's configured chunking values and then to
the engine defaults, so for any config a build could actually run the
no-parameter preview shows what that build would do. The preview does NOT
re-validate project config (that stays build-load's job — an unrelated
malformed block must not fail a chunking preview): a chunking value the
build loader would reject (string, null, bool) silently falls back to the
engine default here. The pair relation (0 <= overlap < max_chars) is validated
by ``chunk_text`` itself (the contract documents that JSON Schema cannot
express a two-field numeric comparison); violations answer 400 with the
validator's own message.

Read-only RPC over POST: no Idempotency-Key (there is no effect to replay —
accepting one would falsely signal write semantics; same category as /query/*).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request
from fastapi.routing import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from api.deps import Conn, response_meta
from api.envelope import success
from api.errors import ApiError, ErrorCode
from api.registry_errors import translate_registry_error
from core.clean.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP, chunk_text
from core.registry import ProjectNotFoundError, get_project
from core.stores import tables
from core.stores.repo import BuildScopedRepo, NoActiveBuildError
from core.stores.repo import resolve_active_binding as _resolve_active_binding

router = APIRouter(tags=["clean"])


class CleanPreviewRequest(BaseModel):
    """Mirror of the contract's CleanPreviewRequest — extra keys are rejected
    (a misspelled ``max_char`` silently falling back to defaults would preview
    the WRONG chunking and the operator would trust it)."""

    model_config = ConfigDict(extra="forbid")

    max_chars: int | None = Field(default=None, ge=1)
    overlap: int | None = Field(default=None, ge=0)
    document_id: uuid.UUID | None = None
    text: str | None = Field(default=None, min_length=1)


def _reject(message: str) -> ApiError:
    return ApiError(ErrorCode.VALIDATION_ERROR, message)


async def _document_raw(conn: Any, project: str, document_id: uuid.UUID) -> tuple[str, uuid.UUID]:
    """The document's raw text from the ACTIVE build, with the build that served it.

    A real project with no active build is 409 NO_ACTIVE_BUILD; only then can a
    document id miss (404-status via the coarse frozen code — ids are minted per
    build, so an id from a superseded build cannot resolve in the current one).
    """
    try:
        binding = await _resolve_active_binding(conn, project)
    except NoActiveBuildError as exc:
        raise translate_registry_error(exc) from exc
    repo = BuildScopedRepo.bound_to(conn, binding)
    rows = await repo.fetch_all(tables.documents, tables.documents.c.id == document_id)
    if not rows:
        # same GAP as the inspect reads (their module docstring): true 404
        # status, coarse frozen code — the enum has no inspect not-found code,
        # and clients (FE3 precedent) branch on the STATUS for this reason.
        raise HTTPException(status_code=404, detail=f"document {document_id} not found")
    raw = rows[0].raw
    if not raw:
        # documents.raw is nullable; chunking nothing would answer an empty
        # preview that reads as "these parameters produce no chunks" — the
        # exact wrong conclusion about the parameters. Fail loud instead.
        raise _reject(f"document {document_id} has no raw text to chunk")
    return raw, binding.build_id


async def _project_chunking_defaults(conn: Any, project: str) -> tuple[int, int]:
    """The project's configured chunking values (engine defaults where unset),
    with the missing-project 404 FIRST — same precedence as every project
    endpoint (a preview against a nonexistent project must not answer with
    engine defaults as if it existed)."""
    proj = await get_project(conn, project)
    if proj is None:
        raise translate_registry_error(ProjectNotFoundError(project))
    block = proj.config.get("chunking") if isinstance(proj.config, dict) else None
    max_chars = DEFAULT_MAX_CHARS
    overlap = DEFAULT_OVERLAP

    def _int(value: Any) -> bool:
        # bool is an int subclass; a config of `max_chars: true` must fall back,
        # not preview with max_chars=1
        return isinstance(value, int) and not isinstance(value, bool)

    if isinstance(block, dict):
        if _int(block.get("max_chars")):
            max_chars = block["max_chars"]
        if _int(block.get("overlap")):
            overlap = block["overlap"]
    return max_chars, overlap


@router.post("/projects/{project}/clean/preview")
async def preview_clean_endpoint(
    request: Request, conn: Conn, body: CleanPreviewRequest, project: str
) -> dict[str, Any]:
    if (body.document_id is None) == (body.text is None):
        raise _reject("provide exactly one source: document_id or text")

    # project 404 first (both sources), and the config fallbacks in one read
    default_max, default_overlap = await _project_chunking_defaults(conn, project)
    max_chars = body.max_chars if body.max_chars is not None else default_max
    overlap = body.overlap if body.overlap is not None else default_overlap

    build_id: uuid.UUID | None = None
    if body.document_id is not None:
        raw, build_id = await _document_raw(conn, project, body.document_id)
    else:  # the exactly-one gate above guarantees text is present here
        raw = body.text or ""

    try:
        chunks = chunk_text(raw, max_chars=max_chars, overlap=overlap)
    except ValueError as exc:
        # chunk_text owns the pair relation (0 <= overlap < max_chars) the
        # schema cannot express; its message names both offending values.
        raise _reject(str(exc)) from exc

    return success(
        {
            "chunks": [
                {
                    "ordinal": c.ordinal,
                    "text": c.text,
                    "start_offset": c.start_offset,
                    "end_offset": c.end_offset,
                    "token_count": c.token_count,
                }
                for c in chunks
            ]
        },
        **response_meta(request),
        build_id=build_id,
    )
