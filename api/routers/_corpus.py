"""Managed-corpus path rules shared by the routers that create or write one.

Sibling of ``_query.py``: a shared underscore-prefixed MODULE exporting public
names, rather than one router importing another router's private symbol.

Lives in ``api`` rather than ``core`` because the rules it enforces are core's
(``safe_project_subdir``, ``ensure_resolvable_file_uri``) but the refusal it
raises is the API layer's frozen ``ApiError`` envelope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.errors import ApiError, ErrorCode
from core.builds.sources import SourceResolutionError, ensure_resolvable_file_uri
from core.paths import safe_project_subdir


def reject_unsafe_corpus_path(settings: Any, project: str) -> None:
    """Raise a 400 if the project name can't back a resolvable managed corpus.

    The project name is a path component of ``upload_corpus_dir``. Two failure
    modes, both a 400 BEFORE any file I/O:

    * a name like ``..`` or one with separators would let the corpus escape the
      root, writing generated files outside it AND registering that escaped dir
      as the canonical source (a later build could then ingest unrelated local
      files) — delegated to the shared ``safe_project_subdir`` (the guard the
      eval worker uses);
    * a name that IS a safe path component but whose corpus ``as_uri()`` encodes
      to a form the source resolver rejects (``foo:bar`` → ``%3A``, ``foo|bar``
      → ``|``) — the upload would register a managed source EVERY later build
      then fails to resolve. ``ensure_resolvable_file_uri`` applies the exact
      source-resolution rules, so the name is refused at capture rather than
      accepted into an unbuildable source.

    Called at BOTH ends of the corpus's life (QA10/Codex #149): by the upload
    endpoint before it writes, and by project creation before the row exists.
    Only the second is new — the first surface was already guarded, and
    checking at creation is what stops a project being created and *then*
    discovering that every upload to it 400s forever.

    Kept SYNC so the filesystem-touching ``resolve()`` stays off the async
    endpoints' blocking-call lint.
    """
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
