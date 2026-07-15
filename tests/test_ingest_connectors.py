"""Why: connectors are where source identity is minted. content_hash must be
stable under everything that ISN'T content (file location, CSV column order),
because §18 keys rerun line-up on it; and a structured row must carry its
citable identity (table + pk, §27.2) from the very first step or it can never
be cited at query time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.ingest.connectors import (
    DocumentPayload,
    content_hash,
    read_csv_rows,
    read_text_documents,
)


def test_content_hash_tracks_content_only() -> None:
    """§18: item_ref = content_hash. Two payloads with the same text ARE the
    same document even from different uris; different text never collides."""
    assert content_hash("same") == content_hash("same")
    assert content_hash("a") != content_hash("b")


def test_text_connector_selects_accepts_and_orders(tmp_path: Path) -> None:
    """Deterministic sorted order (rerun line-up starts at the connector),
    suffix→mime mapping, and out-of-scope files are not selected — they are
    not 'skipped items', they were never work."""
    (tmp_path / "b.md").write_text("beta", encoding="utf-8")
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("print()", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "c.TXT").write_text("gamma", encoding="utf-8")

    payloads = list(read_text_documents(tmp_path))
    assert [p.raw for p in payloads] == ["alpha", "beta", "gamma"]  # sorted, recursive
    assert [p.mime for p in payloads] == ["text/plain", "text/markdown", "text/plain"]
    assert all(p.source_uri.startswith("file://") for p in payloads)
    assert payloads[0].metadata == {"filename": "a.txt"}


def test_text_connector_refuses_a_missing_root(tmp_path: Path) -> None:
    """A typo'd source path is a configuration failure, not an empty source —
    silently yielding nothing would 'succeed' an ingest of zero documents."""
    with pytest.raises(NotADirectoryError):
        list(read_text_documents(tmp_path / "nope"))


def test_managed_source_ingests_only_registered_files(tmp_path: Path) -> None:
    """A managed upload source's registered file list (a NON-EMPTY stash) is
    AUTHORITATIVE: only registered files are ingested. WHY: a file left in the
    scanned corpus by a failed / rolled-back upload (present on disk but never
    registered) must never be ingested with fallback metadata — a rejected/failed
    upload cannot be allowed to affect a later build's results."""
    (tmp_path / "registered.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "orphan.txt").write_text("leaked", encoding="utf-8")  # on disk, unregistered
    stash = {"registered.txt": {"context": {"title": "Kept"}}}

    payloads = list(read_text_documents(tmp_path, stash))

    assert [p.raw for p in payloads] == ["keep"]  # the orphan is NOT ingested
    assert payloads[0].metadata == {"context": {"title": "Kept"}}  # the registered envelope
    # sanity: with NO stash (a plain directory source) both files ARE read — the
    # restrict-to-registered rule is scoped to managed sources only
    assert {p.raw for p in read_text_documents(tmp_path)} == {"keep", "leaked"}


def test_csv_rows_carry_citable_identity_and_canonical_content(tmp_path: Path) -> None:
    """§27.2 row refs cite table + pk, so both are minted at ingest; raw is
    canonical JSON (sorted keys) so a re-export with reordered columns does
    not mint new content hashes for unchanged rows."""
    csv_path = tmp_path / "people.csv"
    csv_path.write_text("id,name\n7,Alice\n9,Bob\n", encoding="utf-8")
    reordered = tmp_path / "people_reordered.csv"
    reordered.write_text("name,id\nAlice,7\nBob,9\n", encoding="utf-8")

    rows = list(read_csv_rows(csv_path, table="people", pk_column="id"))
    assert [p.metadata for p in rows] == [
        {"table": "people", "pk": "7"},
        {"table": "people", "pk": "9"},
    ]
    assert all(p.mime == "application/json" for p in rows)
    assert rows[0].source_uri.endswith("#id=7")
    # column order is not content: same rows -> same hashes
    again = list(read_csv_rows(reordered, table="people", pk_column="id"))
    assert [content_hash(p.raw) for p in rows] == [content_hash(p.raw) for p in again]


def test_csv_rows_refuse_missing_empty_or_duplicated_pk(tmp_path: Path) -> None:
    """A row without a pk could never be cited (§27.2), and a DUPLICATED pk
    makes the (table, pk) citation ambiguous — both are refused at the door,
    not discovered broken at query time."""
    no_column = tmp_path / "no_pk.csv"
    no_column.write_text("name\nAlice\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pk column"):
        list(read_csv_rows(no_column, table="t", pk_column="id"))
    empty_value = tmp_path / "empty_pk.csv"
    empty_value.write_text("id,name\n , Alice\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty pk"):
        list(read_csv_rows(empty_value, table="t", pk_column="id"))
    duplicated = tmp_path / "dup_pk.csv"
    duplicated.write_text("id,name\n7,Alice\n7,Bob\n", encoding="utf-8")
    with pytest.raises(ValueError, match="repeats pk"):
        list(read_csv_rows(duplicated, table="t", pk_column="id"))
    # the table name is the OTHER half of the §27.2 citation — same rule
    ok = tmp_path / "ok.csv"
    ok.write_text("id\n1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="table name"):
        list(read_csv_rows(ok, table="  ", pk_column="id"))


def test_payloads_default_to_empty_metadata() -> None:
    """The dataclass default is a fresh dict per instance — a shared mutable
    default would leak metadata across unrelated documents."""
    one = DocumentPayload("u1", "r1", "text/plain")
    two = DocumentPayload("u2", "r2", "text/plain")
    assert one.metadata == {} and two.metadata == {}
    assert one.metadata is not two.metadata
