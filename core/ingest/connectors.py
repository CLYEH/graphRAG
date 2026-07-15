"""Source connectors: external data → uniform document payloads (DESIGN §5 step 1, C2).

Two §2 source families feed the pipeline: free-text documents and structured
(tabular) data. Both normalize to :class:`DocumentPayload` here, so the rest
of ingest — hashing, dedup, persistence — is connector-agnostic. Structured
rows become one payload each, carrying ``{"table", "pk"}`` metadata because
§27.2's ``row`` source refs must later cite ``table + pk``; a row that loses
its pk at ingest could never be cited at query time.

``content_hash`` is THE document identity: §18 fixes ``item_ref =
content_hash`` for documents (rerun line-up), and §5's skip/rerun idempotency
compares it. It hashes the raw text only — metadata/source_uri may change
(file moved, re-exported) without making the content a "new" document.

Connectors are synchronous and yield lazily: sources can be large, and the
async persistence boundary is `core.ingest.documents`, not file IO.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.stores.tables import STRUCTURED_MIME

#: Free-text suffixes the document connector accepts, mapped to their mime
#: type. Extend as real projects need more formats (PDF etc. arrive with
#: their own extraction dependencies — deliberately not in C2).
TEXT_SUFFIXES: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
}


@dataclass(frozen=True)
class DocumentPayload:
    """One ingestable unit, whatever the source (§5 step 1)."""

    source_uri: str
    raw: str
    mime: str
    metadata: dict[str, Any] = field(default_factory=dict)


def content_hash(raw: str) -> str:
    """The stable document identity (§18 item_ref; §5 idempotency key)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_text_documents(
    root: Path, metadata_by_filename: Mapping[str, dict[str, Any]] | None = None
) -> Iterator[DocumentPayload]:
    """Yield one payload per accepted text file under ``root`` (recursive).

    Deterministic order (sorted paths) so two runs over the same tree produce
    the same payload sequence — rerun line-up starts at the connector. Files
    with unaccepted suffixes are silently not SELECTED (they are out of scope
    by definition, not failed items); an accepted file that cannot be decoded
    raises — a corrupt source is a loud failure, not a skipped item.

    ``metadata_by_filename`` carries the DR-010 metadata envelope captured at
    upload time, keyed by the file's stored (on-disk) name — the managed-source
    stash the ingest connector threads onto ``documents.metadata`` (UXC1b
    capture → persist). When it is NON-EMPTY the source is a MANAGED one whose
    registered file list is AUTHORITATIVE: only those files are ingested, so a
    file left in the scanned corpus by a failed / rolled-back upload (present on
    disk but never registered) is never ingested with fallback metadata — the
    managed source's durable file list, not the raw directory contents, decides
    what belongs to it. An EMPTY/absent stash is a plain directory source: every
    accepted file is read, each falling back to the connector-derived
    ``{"filename": ...}`` — the original behavior for a non-upload source or a
    file placed on disk directly.
    """
    if not root.is_dir():
        raise NotADirectoryError(f"document source root {root} is not a directory")
    stash = metadata_by_filename or {}
    registered_only = bool(stash)  # a managed source: its file list is authoritative
    for path in sorted(root.rglob("*")):
        suffix = path.suffix.lower()
        if not path.is_file() or suffix not in TEXT_SUFFIXES:
            continue
        if registered_only and path.name not in stash:
            continue  # an orphan not in the managed source's registered files — skip
        yield DocumentPayload(
            source_uri=path.resolve().as_uri(),
            raw=path.read_text(encoding="utf-8"),
            mime=TEXT_SUFFIXES[suffix],
            metadata=stash.get(path.name, {"filename": path.name}),
        )


def read_csv_rows(path: Path, *, table: str, pk_column: str) -> Iterator[DocumentPayload]:
    """Yield one payload per CSV row — the structured-source connector (§2/§6).

    Each row's raw text is its canonical JSON (sorted keys, so column order
    changes don't mint new content hashes). ``table`` and the row's pk land
    in metadata because §27.2 row refs cite ``table + pk`` — and the pk must
    exist and be non-empty NOW: a row that cannot be cited later is refused
    at the door, not discovered broken at query time.
    """
    if not table.strip():
        raise ValueError(
            "table name must be non-empty — §27.2 row refs cite table + pk, "
            "and rows ingested under a blank table could never be cited"
        )
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or pk_column not in reader.fieldnames:
            raise ValueError(
                f"pk column {pk_column!r} not in {path} header "
                f"(found: {list(reader.fieldnames or [])})"
            )
        seen: set[str] = set()
        for index, row in enumerate(reader):
            pk = (row.get(pk_column) or "").strip()
            if not pk:
                raise ValueError(
                    f"row {index} of {path} has an empty pk ({pk_column!r}) — "
                    "it could never be cited as a §27.2 row source ref"
                )
            if pk in seen:
                raise ValueError(
                    f"row {index} of {path} repeats pk {pk!r} — a duplicated "
                    "(table, pk) makes the §27.2 row source ref ambiguous "
                    f"(mis-declared pk column, or dirty data)"
                )
            seen.add(pk)
            yield DocumentPayload(
                source_uri=f"{path.resolve().as_uri()}#{pk_column}={pk}",
                raw=json.dumps(row, ensure_ascii=False, sort_keys=True),
                mime=STRUCTURED_MIME,
                metadata={"table": table, "pk": pk},
            )
