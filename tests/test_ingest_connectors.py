"""Why: connectors are where source identity is minted. content_hash must be
stable under everything that ISN'T content (file location, CSV column order),
because §18 keys rerun line-up on it; and a structured row must carry its
citable identity (table + pk, §27.2) from the very first step or it can never
be cited at query time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.ingest.connectors import (
    XLSX_BLANK_ROW_STOP,
    DocumentPayload,
    content_hash,
    read_csv_rows,
    read_text_documents,
    read_xlsx_rows,
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


def test_managed_source_with_an_empty_registered_list_ingests_nothing(tmp_path: Path) -> None:
    """Presence — not truthiness — is the managed signal. A managed source with an
    EMPTY registered map ingests NOTHING (an empty authoritative list), never a
    directory scan: a malformed/emptied managed source must not silently fall back
    to reading UNREGISTERED files. An explicit None, by contrast, is a plain
    directory source. WHY: this is the class-of-bug where a managed source degrades
    to a directory scan and ingests orphan files the registered list excluded."""
    (tmp_path / "orphan.txt").write_text("leaked", encoding="utf-8")  # on disk, unregistered
    assert list(read_text_documents(tmp_path, {})) == []  # managed + empty → nothing
    # None (the default / absent stash) still means a plain directory scan
    assert {p.raw for p in read_text_documents(tmp_path, None)} == {"leaked"}


def test_managed_source_fails_loudly_on_a_missing_registered_file(tmp_path: Path) -> None:
    """The registered file list is authoritative in BOTH directions: a file the
    managed source LISTS but that is absent from disk (a lost write / disk loss)
    fails LOUDLY. WHY: the SoR says the upload was accepted, so silently ingesting
    fewer documents than registered would corrupt results with no signal."""
    (tmp_path / "present.txt").write_text("here", encoding="utf-8")
    stash: dict[str, dict[str, Any]] = {
        "present.txt": {"context": {}},
        "gone.txt": {"context": {}},
    }  # gone.txt absent
    with pytest.raises(FileNotFoundError, match="gone.txt"):
        list(read_text_documents(tmp_path, stash))


def test_managed_source_rejects_untrusted_traversal_file_names(tmp_path: Path) -> None:
    """A managed source's registered names are UNTRUSTED — a text source's
    managed-file stash is stored as-is by the sources API — so a name with a path
    separator / '..' / absolute path is REFUSED before any file read. WHY: the
    connector joins each name to a filesystem root; a name like '../secret.md' or
    '/etc/passwd' would read OUTSIDE the source root (a build ingesting unrelated
    local files). The guard must fire at the door, never open the escaping path."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "ok.txt").write_text("fine", encoding="utf-8")
    # a real sibling OUTSIDE `root` but inside the isolated tmp_path (never the
    # shared pytest tmp parent) that '../secret.txt' would target if unguarded
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")
    # 'a\\b.txt' carries a literal backslash: on POSIX (the deployment) it is a
    # legal filename char that Path treats as NON-separator, so only the explicit
    # "\\" in name clause rejects it — the Windows-separator branch, exercised here.
    for bad in ("../secret.txt", "sub/ok.txt", "/etc/passwd", "..", ".", "a\\b.txt"):
        stash: dict[str, dict[str, Any]] = {bad: {"context": {}}, "ok.txt": {"context": {}}}
        with pytest.raises(ValueError, match="bare in-root|outside the source root"):
            [p.raw for p in read_text_documents(root, stash)]


def test_managed_source_refuses_a_symlink_escaping_the_root(tmp_path: Path) -> None:
    """Defense in depth: even a BARE in-root name must not read outside root via a
    symlink. The connector resolves the path and refuses one whose real parent is
    not the source root, so a symlink planted in the corpus pointing at an external
    secret cannot leak into a build. WHY: the name check alone would pass a bare
    'link.txt'; the resolve()-under-root backstop is what closes the symlink vector."""
    root = tmp_path / "corpus"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")  # outside root
    link = root / "link.txt"
    try:
        link.symlink_to(secret)  # a bare in-root NAME whose target escapes root
    except OSError:  # pragma: no cover - Windows without the symlink privilege
        pytest.skip("symlink creation not permitted on this platform")
    with pytest.raises(ValueError, match="outside the source root"):
        list(read_text_documents(root, {"link.txt": {"context": {}}}))


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


# ---- xlsx connector (SRC1) ---------------------------------------------------


def _write_xlsx(path: Path, header: list[Any], rows: list[list[Any]]) -> Path:
    """A minimal workbook fixture — rows carry TYPED cells so the tests can
    bake in the real dirt (float ids, None cells) exactly as openpyxl reads it
    back from authored files."""
    import openpyxl
    from openpyxl.worksheet.worksheet import Worksheet

    wb = openpyxl.Workbook()
    ws = wb.active
    assert isinstance(ws, Worksheet)
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_xlsx_rows_render_the_mapped_columns_with_per_row_citation(tmp_path: Path) -> None:
    """Why: the column MAPPING comes from source metadata — no vocabulary is
    hard-coded — and every row must mint a citable per-row identity
    (source_uri #row=) the moment it is read, or it can never be traced back
    to its spreadsheet row at query time."""
    path = _write_xlsx(
        tmp_path / "guide.xlsx",
        ["編號", "標題", "內容詳情", "位置", "分類"],
        [
            [1, "深海探索廳", "介紹深潛器。", "B1", "常設展"],
            [2, "海洋劇場", "球幕電影。", "", "設施"],  # blank extra is omitted, not rendered empty
        ],
    )
    payloads = list(
        read_xlsx_rows(
            path,
            title_column="標題",
            body_column="內容詳情",
            id_column="編號",
            extra_columns=("位置", "分類"),
            label="導覽",
        )
    )
    assert [p.source_uri for p in payloads] == [
        f"{path.resolve().as_uri()}#row=1",
        f"{path.resolve().as_uri()}#row=2",
    ]
    assert payloads[0].raw == "【導覽】深海探索廳\n位置:B1\n分類:常設展\n\n介紹深潛器。\n"
    assert payloads[1].raw == "【導覽】海洋劇場\n分類:設施\n\n球幕電影。\n"
    assert all(p.mime == "text/plain" for p in payloads)
    assert payloads[0].metadata == {"filename": "guide.xlsx", "row": "1"}


def test_xlsx_headers_normalize_annotations_and_ids_normalize_floats(tmp_path: Path) -> None:
    """Why: real workbooks annotate headers (問題(必填) — full- OR half-width
    parens) and hand back integral ids as floats (7.0). The mapping must hit
    the AUTHORED column name, and the citation must carry the id the author
    typed — '7', never '7.0'."""
    # one FULL-width pair and one half-width pair. Full- and half-width parens
    # are visually identical in most fonts and a "full-width" spelling silently
    # degraded to half-width once (reviewer catch), so the fixture VERIFIES its
    # own codepoints — a re-mangled fixture fails here, never false-greens.
    full_width_header = "問題（必填）"
    # the escape spelling is plain ASCII in this source file — it cannot be
    # mangled, so it discriminates a degraded literal above
    assert "\uff08" in full_width_header and "\uff09" in full_width_header
    path = _write_xlsx(
        tmp_path / "faq.xlsx",
        ["編號", "主題", full_width_header, "答案(必填)"],
        [[7.0, "服務", "開放時間?", "每日九點。"]],
    )
    payloads = list(
        read_xlsx_rows(
            path,
            title_column="問題",
            body_column="答案",
            id_column="編號",
            extra_columns=("主題",),
            label="常見問題",
        )
    )
    assert len(payloads) == 1
    assert payloads[0].source_uri.endswith("#row=7")
    assert payloads[0].raw == "【常見問題】開放時間?\n主題:服務\n\n每日九點。\n"


def test_xlsx_skips_template_rows_and_stops_after_the_blank_streak(tmp_path: Path) -> None:
    """Why: authored workbooks carry template tails — pre-numbered rows with no
    content, long runs of empty formatting rows — and the sheet's self-reported
    dimension can lie outright (a pilot file claimed ~1M rows). A no-content
    row is skipped, and a blank streak ends the scan so data beyond the
    threshold is deliberately unreachable (a content gap that long is a tail,
    not data)."""
    rows: list[list[Any]] = [
        [1, "有內容", "正文"],
        [2, "", ""],  # pre-numbered template row: nothing to render → skipped
        *([[None, None, None]] * XLSX_BLANK_ROW_STOP),  # the tail
        [99, "掃描不到", "在斷點之後"],
    ]
    path = _write_xlsx(tmp_path / "tail.xlsx", ["編號", "標題", "內容詳情"], rows)
    payloads = list(
        read_xlsx_rows(path, title_column="標題", body_column="內容詳情", id_column="編號")
    )
    assert [p.metadata["row"] for p in payloads] == ["1"]


def test_xlsx_blank_id_falls_back_to_ordinal_and_duplicates_fail_loud(tmp_path: Path) -> None:
    """Why: real files leave 編號 blank mid-sheet — refusing them would block
    whole corpora, so a blank id falls back to the 1-based data-row ordinal.
    But a DUPLICATED id makes #row= ambiguous (two rows, one citation) and
    fails loud, the same rule as the CSV pk."""
    path = _write_xlsx(
        tmp_path / "blank_id.xlsx",
        ["編號", "標題", "內容詳情"],
        [[None, "甲", "內文一"], [None, "乙", "內文二"]],
    )
    payloads = list(
        read_xlsx_rows(path, title_column="標題", body_column="內容詳情", id_column="編號")
    )
    assert [p.metadata["row"] for p in payloads] == ["1", "2"]

    dup = _write_xlsx(
        tmp_path / "dup_id.xlsx",
        ["編號", "標題", "內容詳情"],
        [[5, "甲", "內文一"], [5.0, "乙", "內文二"]],  # 5 and 5.0 canonicalize to the SAME id
    )
    with pytest.raises(ValueError, match="repeats id"):
        list(read_xlsx_rows(dup, title_column="標題", body_column="內容詳情", id_column="編號"))


def test_xlsx_duplicate_mapped_header_fails_loud_but_unmapped_duplicates_pass(
    tmp_path: Path,
) -> None:
    """Why: two headers that NORMALIZE to the same mapped name (問題(必填) and
    問題) are ambiguous — the mapping names only the normalized header, so it
    cannot say which column is meant, and a first-wins bind would silently
    render the wrong column (Codex #85). The dual: duplicates among UNMAPPED
    columns stay tolerated — the render never reads them, and refusing the
    workbook for decorative junk would over-block real files."""
    ambiguous = _write_xlsx(
        tmp_path / "dup_mapped.xlsx",
        ["編號", "問題(必填)", "問題", "答案"],
        [[1, "甲", "乙", "內文"]],
    )
    with pytest.raises(ValueError, match="more than once"):
        list(read_xlsx_rows(ambiguous, title_column="問題", body_column="答案"))

    # the over-block dual: 備註(一)/備註(二) collide after normalization but are
    # unmapped — the workbook still ingests
    tolerated = _write_xlsx(
        tmp_path / "dup_unmapped.xlsx",
        ["編號", "標題", "內容", "備註(一)", "備註(二)"],
        [[1, "甲", "內文", "x", "y"]],
    )
    payloads = list(read_xlsx_rows(tolerated, title_column="標題", body_column="內容"))
    assert len(payloads) == 1


def test_xlsx_missing_mapped_column_and_empty_sheet_fail_loud(tmp_path: Path) -> None:
    """Why: a mapping that names a column the workbook doesn't have would
    otherwise render blank documents forever — refuse at the door, naming the
    headers that DO exist so the operator can fix the mapping."""
    path = _write_xlsx(tmp_path / "cols.xlsx", ["標題", "內容"], [["甲", "內文"]])
    with pytest.raises(ValueError, match="mapped column"):
        list(read_xlsx_rows(path, title_column="標題", body_column="內容詳情"))
    with pytest.raises(ValueError, match="title_column must be non-empty"):
        list(read_xlsx_rows(path, title_column="  ", body_column="內容"))

    import openpyxl

    empty = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(empty)
    with pytest.raises(ValueError, match="empty"):
        list(read_xlsx_rows(empty, title_column="標題", body_column="內容"))
