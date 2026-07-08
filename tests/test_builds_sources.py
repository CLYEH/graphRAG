"""Why: ``resolve_source`` is the routing layer between a project's free-form
``sources`` rows and the C2 connectors. A source that can't be routed must fail
LOUD (never a silent empty ingest that leaves a build with nothing), and a
structured source must carry the ``table``/``pk_column`` §27.2 row refs cite.
These hermetic tests pin the dispatch and its fail-loud edges over tmp files.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.builds.sources import SourceResolutionError, resolve_source
from core.registry.store import Source
from core.stores.tables import STRUCTURED_MIME

_NOW = datetime(2026, 1, 1)


def _source(uri: str, *, kind: str | None, metadata: dict[str, Any] | None = None) -> Source:
    return Source(
        id=uuid.uuid4(),
        project="p",
        kind=kind,
        uri=uri,
        metadata=metadata or {},
        added_at=_NOW,
    )


def test_text_source_yields_a_payload_per_text_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.md").write_text("# beta", encoding="utf-8")
    (tmp_path / "skip.bin").write_text("ignored", encoding="utf-8")  # unaccepted suffix

    payloads = list(resolve_source(_source(tmp_path.as_uri(), kind="text")))

    assert {p.raw for p in payloads} == {"alpha", "# beta"}
    assert {p.mime for p in payloads} == {"text/plain", "text/markdown"}


def test_structured_source_yields_a_payload_per_row(tmp_path: Path) -> None:
    csv = tmp_path / "companies.csv"
    csv.write_text("id,name\n1,Acme\n2,Globex\n", encoding="utf-8")

    payloads = list(
        resolve_source(
            _source(
                csv.as_uri(),
                kind="structured",
                metadata={"table": "companies", "pk_column": "id"},
            )
        )
    )

    assert len(payloads) == 2
    assert all(p.mime == STRUCTURED_MIME for p in payloads)
    assert [p.metadata["pk"] for p in payloads] == ["1", "2"]
    assert all(p.metadata["table"] == "companies" for p in payloads)


@pytest.mark.parametrize("kind", [None, "url", "database", "csv", "documents"])
def test_unsupported_kind_fails_loud(tmp_path: Path, kind: str | None) -> None:
    # a source no connector handles must NOT silently ingest zero documents.
    with pytest.raises(SourceResolutionError, match="unsupported kind"):
        list(resolve_source(_source(tmp_path.as_uri(), kind=kind)))


def test_non_file_uri_is_rejected() -> None:
    # only file-backed sources are wired; a bare path or http uri fails loud
    # (a Windows drive letter would also mis-parse as a scheme).
    with pytest.raises(SourceResolutionError, match="not a file:// URI"):
        list(resolve_source(_source("https://example.com/data.csv", kind="structured")))


def test_structured_missing_table_or_pk_column_fails_loud(tmp_path: Path) -> None:
    csv = tmp_path / "t.csv"
    csv.write_text("id,name\n1,x\n", encoding="utf-8")
    with pytest.raises(SourceResolutionError, match="'table'"):
        list(resolve_source(_source(csv.as_uri(), kind="structured", metadata={"pk_column": "id"})))
    with pytest.raises(SourceResolutionError, match="'pk_column'"):
        list(resolve_source(_source(csv.as_uri(), kind="structured", metadata={"table": "t"})))


def test_structured_non_string_or_blank_meta_fails_loud(tmp_path: Path) -> None:
    csv = tmp_path / "t.csv"
    csv.write_text("id,name\n1,x\n", encoding="utf-8")
    with pytest.raises(SourceResolutionError, match="'table'"):
        list(
            resolve_source(
                _source(
                    csv.as_uri(), kind="structured", metadata={"table": "  ", "pk_column": "id"}
                )
            )
        )
    with pytest.raises(SourceResolutionError, match="'pk_column'"):
        list(
            resolve_source(
                _source(csv.as_uri(), kind="structured", metadata={"table": "t", "pk_column": 1})
            )
        )
