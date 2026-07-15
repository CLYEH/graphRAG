"""Why: ``resolve_source`` is the routing layer between a project's free-form
``sources`` rows and the C2 connectors. A source that can't be routed must fail
LOUD (never a silent empty ingest that leaves a build with nothing), and a
structured source must carry the ``table``/``pk_column`` §27.2 row refs cite.
These hermetic tests pin the dispatch and its fail-loud edges over tmp files.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.builds.sources import SourceResolutionError, _local_path, resolve_source
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


def test_managed_text_source_threads_envelopes_onto_payloads(tmp_path: Path) -> None:
    """A managed text source (``metadata['files']`` present) routes each stored
    file's DR-010 envelope onto its payload — the capture→persist path UXC1b needs."""
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    payloads = list(
        resolve_source(
            _source(
                tmp_path.as_uri(),
                kind="text",
                metadata={"files": {"a.txt": {"context": {"title": "A"}}}},
            )
        )
    )
    assert [p.raw for p in payloads] == ["alpha"]
    assert payloads[0].metadata == {"context": {"title": "A"}}


@pytest.mark.parametrize("bad_files", [[], None, "x", 5])
def test_managed_text_source_rejects_a_non_object_files_value(
    tmp_path: Path, bad_files: Any
) -> None:
    """A PRESENT ``files`` key marks the source managed; a non-object value (``[]``,
    ``null``, a string, a number — all storable in free-form source metadata) is
    malformed and must fail LOUD. Returning None here would send resolve_source down
    the unmanaged directory-scan path, ingesting unregistered orphan files. Presence
    of the key, not the value's truthiness, is what distinguishes managed from plain."""
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    with pytest.raises(SourceResolutionError, match="non-object 'files'"):
        list(resolve_source(_source(tmp_path.as_uri(), kind="text", metadata={"files": bad_files})))


def test_text_source_without_a_files_key_scans_as_a_directory(tmp_path: Path) -> None:
    """The COUNTERPART to the malformed-value rejection: an ABSENT files key is a
    plain (non-upload) text source — scanned as a directory, not rejected. This pins
    the present-vs-absent distinction so a future refactor can't collapse them."""
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    payloads = list(resolve_source(_source(tmp_path.as_uri(), kind="text", metadata={"other": 1})))
    assert [p.raw for p in payloads] == ["alpha"]
    assert payloads[0].metadata == {"filename": "a.txt"}  # connector-derived fallback


def test_managed_text_source_rejects_a_malformed_files_entry(tmp_path: Path) -> None:
    """A managed text source's ``metadata['files']`` maps each stored name to its
    DR-010 envelope OBJECT. A non-object entry must fail LOUD: silently dropping it
    (and, if every entry drops, leaving ``{}`` that the connector reads as a plain
    directory) would scan and ingest UNREGISTERED orphan files the authoritative
    managed list was supposed to exclude — a rejected/failed upload affecting a build."""
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    with pytest.raises(SourceResolutionError, match="non-object metadata entry"):
        list(
            resolve_source(
                _source(
                    tmp_path.as_uri(),
                    kind="text",
                    metadata={"files": {"a.txt": "not-an-object"}},
                )
            )
        )


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
        # url2pathname's first act is replace(":", "|"), so ":" and "|" are the SAME
        # character to it and the letter before the first one becomes a drive. A pipe
        # anywhere: file:///a|/corpus reads "A:\\corpus".
        ("file:///a|/corpus", "pipe"),
        ("file:///data/a%7Cb", "pipe"),
        # ...and so does a colon outside the drive position — an ordinary filename
        # silently re-roots the path onto another volume (/data/foo:bar → "O:bar",
        # /data:x/y → "A:x\\y"). The last two would otherwise escape as a raw OSError
        # ("Bad URL: /C|/data/foo|bar"), not a SourceResolutionError.
        ("file:///data/foo:bar", "colon"),
        ("file:///data:x/y", "colon"),
        ("file:///C:/data/foo:bar", "colon"),
        ("file:///1:/data", "colon"),
        # url2pathname decides STRUCTURE on the still-encoded path (it detects the
        # drive from a literal ":"), while the segment checks run on the decoded one —
        # so an encoded drive colon passes them and yet reads "\\C:\\corpus" (no drive)
        ("file:///C%3A/corpus", "encodes the drive separator"),
        # single-slash: reads the SAME path as the triple-slash form, but the Console
        # gate refuses the shape — so a source registered this way via API/CLI would be
        # buildable from the CLI and never from the UI. One canonical shape, one accept
        # set on both sides of the API.
        ("file:/data/corpus", "triple-slash"),
        ("file:/C:/corpus", "triple-slash"),
        # a malformed escape is LITERAL to unquote (which never raises) but THROWS in
        # the Console's decodeURIComponent — same split enforcement, and it aliases:
        # "/data/100%" and the canonical "/data/100%25" read the same path
        ("file:///data/100%", "malformed percent-escape"),
        ("file:///data/%zz", "malformed percent-escape"),
        # a bare drive is DRIVE-RELATIVE — Path("C:") is the worker's current directory
        # on that drive, not the drive root (the Windows spelling of the cwd hazard)
        ("file:///C:", "bare drive"),
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
    # Two invariants, both enforced HERE because CLI/API/MCP-triggered builds have no
    # Console gate in front of them:
    #   1. display == read — a build over a reinterpreted uri ingests the WRONG tree
    #      (silent wrong data, strictly worse than this loud failure). Most cases below.
    #   2. one canonical shape — the last cases read exactly what they display, but the
    #      Console refuses their shape, so accepting them here would split enforcement:
    #      a source buildable from the CLI and never from the UI.
    with pytest.raises(SourceResolutionError, match=why):
        list(resolve_source(_source(uri, kind="text")))


def test_windows_drive_uri_is_accepted() -> None:
    # class-9 dual for the colon rule: the canonical Windows drive form is what
    # Path.as_uri() emits on a Windows worker, and url2pathname resolves it to exactly
    # the displayed path — it must stay registerable. Pinned on _local_path directly
    # because tmp_path.as_uri() carries no colon at all on POSIX CI, leaving the
    # accept side of the rule unexercised there. The drive must be ANCHORED, which is
    # what makes this pin able to FAIL: a driveless "\\C:\\corpus" (what an encoded
    # drive colon produced before %3A was banned) still CONTAINS "C:", so a substring
    # assertion would be false-green against exactly the regression this guards —
    # nturl2path's drive handling being reworked in CPython 3.14+.
    resolved = _local_path(_source("file:///C:/corpus", kind="text"))
    if os.name == "nt":
        assert resolved.drive == "C:"  # "" for the driveless "\\C:\\corpus"
        assert str(resolved) == "C:" + os.sep + "corpus"
    else:
        # POSIX url2pathname is literally unquote(): the read IS the displayed path
        assert str(resolved) == "/C:/corpus"


_GATE_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "canonical_file_uri.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", _GATE_CORPUS["reject"], ids=lambda c: c["uri"])
def test_gate_corpus_rejects(case: dict[str, str]) -> None:
    # Parity half: this corpus is asserted against the CONSOLE gate too
    # (web/src/pages/fileUriGate.test.ts). The two gates must accept exactly the same
    # set — when they drift, a source is buildable from one side and permanently
    # unrunnable from the other, which is what two of the six Codex findings on PR #71
    # were. Per-side tests cannot see that: each gate was self-consistent and they
    # disagreed. A shared corpus can.
    with pytest.raises(SourceResolutionError):
        list(resolve_source(_source(case["uri"], kind="text")))


@pytest.mark.parametrize("case", _GATE_CORPUS["accept"], ids=lambda c: c["uri"])
def test_gate_corpus_accepts(case: dict[str, str]) -> None:
    # The accept side is what keeps the deny-rules honest (class-9 dual): every canonical
    # form — directory, drive, drive root, a literal "%" or space, a café, canonically
    # encoded — must survive. resolve_source is lazy (the connector reads on iteration),
    # so this exercises _local_path without needing the path to exist.
    if case.get("worker") == "posix" and os.name == "nt":
        # ...except a DRIVELESS path on a Windows worker, which url2pathname roots on
        # whichever drive the process is currently using ("\data\corpus") — so the tree it
        # reads depends on the cwd. The SoR validates the RESOLVED path and refuses it;
        # the Console can't know the worker's OS and accepts it. Pin BOTH halves of that
        # asymmetry, so neither side can drift into silence: the SoR must fail LOUD here.
        with pytest.raises(SourceResolutionError, match="not absolute on this worker"):
            resolve_source(_source(case["uri"], kind="text"))
        return
    resolve_source(_source(case["uri"], kind="text"))


def test_percent_encoded_literal_percent_is_accepted() -> None:
    # class-9 dual for the malformed-escape reject: a directory legitimately named
    # "100%" must stay registerable in its CANONICAL spelling — which is what
    # Path.as_uri() emits ("%25"). Only the malformed spelling is refused. The uri names a
    # drive on a Windows worker because a driveless path is drive-RELATIVE there (the
    # absoluteness rule refuses it) — orthogonal to the decoding this pins.
    uri = "file:///C:/data/100%25" if os.name == "nt" else "file:///data/100%25"
    assert str(_local_path(_source(uri, kind="text"))).endswith("100%")


def test_drive_root_uri_is_accepted() -> None:
    # class-9 dual for the bare-drive reject: only the DRIVE-RELATIVE "file:///C:" is
    # refused. The drive root resolves absolute and must stay registerable — the
    # distinction the reject rests on is exactly is_absolute().
    resolved = _local_path(_source("file:///C:/", kind="text"))
    if os.name == "nt":
        assert resolved.is_absolute() and resolved.drive == "C:"


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
