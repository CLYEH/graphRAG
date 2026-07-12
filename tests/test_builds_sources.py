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


@pytest.mark.parametrize(
    ("uri", "why"),
    [
        # urlsplit strips edge whitespace and tab/newline ANYWHERE, so the worker
        # reads a different path than the stored uri displays
        ("file:///data/corpus ", "whitespace"),
        ("file:///tmp/\t../etc", "control characters"),
        # url2pathname drops the host: file://nas/corpus reads /corpus
        ("file://nas/corpus", "names a host"),
        # query/fragment are stripped from the path the worker reads
        ("file:///data/corpus?old", "query/fragment"),
        ("file:///data/corpus#frag", "query/fragment"),
        # NUL can't name a real file on any filesystem
        ("file:///data/%00corpus", "NUL"),
        # a decoded backslash is a SEPARATOR on a Windows worker (url2pathname),
        # so %2e%2e%5C springs a "..\" traversal the "/"-segment checks can't see
        ("file:///C:/safe/%2e%2e%5CWindows", "backslash"),
        ("file:///data/a%5Cb", "backslash"),
        # an empty path resolves to the worker's cwd; bare "/" is the root
        ("file:", "names no path"),
        ("file://", "names no path"),
        ("file:///", "names no path"),
        # a relative path resolves against the worker's cwd, not what the uri
        # names (and would slip past a leading-slash-assuming segment split)
        ("file:../corpus", "relative path"),
        ("file:relative/corpus", "relative path"),
        # //-leading (raw four-slash) is reinterpreted as UNC root
        ("file:////nas/corpus", "//-leading"),
        # encoded separators hide the segment boundary from the display — no
        # filesystem permits "/" in a filename, so %2F can only be a disguised
        # separator (whether it springs "//", "../", or plain segments)
        ("file:///%2Fdata", "encodes the path separator"),
        ("file:///safe/%2F..%2F..%2Fetc", "encodes the path separator"),
        ("file:///tmp/corpus%2Fprivate", "encodes the path separator"),
        ("file:///a%2fb", "encodes the path separator"),
        # dot segments — raw or percent-encoded — get resolved by the filesystem
        # to a different tree than the display names
        ("file:///data/../etc", "dot path segments"),
        ("file:///data/%2e%2e/etc", "dot path segments"),
    ],
)
def test_non_canonical_file_uri_is_rejected(uri: str, why: str) -> None:
    # the displayed path must be exactly what the worker reads (display==read):
    # a build over a reinterpreted uri ingests the WRONG tree — silent wrong data,
    # strictly worse than this loud failure. CLI/API/MCP-triggered builds have no
    # Console gate in front of them, so the source of truth enforces it.
    with pytest.raises(SourceResolutionError, match=why):
        list(resolve_source(_source(uri, kind="text")))


def test_trailing_slash_directory_uri_is_accepted(tmp_path: Path) -> None:
    # the idiomatic directory form must not be over-blocked (class 9 dual:
    # deny-rules need an acceptance pin)
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    payloads = list(resolve_source(_source(tmp_path.as_uri() + "/", kind="text")))
    assert {p.raw for p in payloads} == {"alpha"}


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
