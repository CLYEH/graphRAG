"""H22 — the TASKS.md as-built archiver and its CI ceiling.

WHY these assertions matter: the loop reads `TASKS.md` on every iteration to
choose the next task, so a completed entry's as-built narrative is a recurring
context tax on a decision it cannot inform. The archiver is what removes it and
the ceiling is what stops it coming back — and both only hold if the split is
lossless, the archive is only ever appended to (never truncated), and the
append is idempotent under a crash.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "archive_task", _ROOT / "scripts" / "archive_task.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["archive_task"] = module
    spec.loader.exec_module(module)
    return module


at = _load()


@pytest.fixture(autouse=True)
def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect BOTH file paths into tmp for every test in this module.

    `archive()` appends to `_ARCHIVE` as a side effect, so a test that calls it
    against the module's real paths writes its fixtures into the repo's own
    archive — and a probe that flips the open mode to 'w' TRUNCATES it. That is
    not hypothetical: it destroyed 78 migrated entries during this task's
    development, and the loss only surfaced because the committed diff was
    implausibly small. Autouse (not opt-in) because the hazard is silent and
    the next test author has no reason to suspect it.
    """
    monkeypatch.setattr(at, "_TASKS", tmp_path / "TASKS.md")
    monkeypatch.setattr(at, "_ARCHIVE", tmp_path / "docs" / "TASKS_ARCHIVE.md")


def _entry(task_id: str, head: str, tail: str) -> str:
    return f"- [x] {task_id} {head}—{tail}"


def test_the_split_is_lossless() -> None:
    """The whole migration rests on this: what leaves TASKS.md must arrive in
    the archive intact. A split that quietly drops a clause would destroy
    history that only exists in this file."""
    tail = "as-built: " + "x" * 400
    line = _entry("T1", "title", tail)
    kept, archived = at.split_entry(line)
    assert archived == tail
    assert kept == "- [x] T1 title"
    # rejoin reconstructs the original exactly (the separator is the only loss)
    assert f"{kept}—{archived}" == line


def test_a_short_entry_is_left_alone() -> None:
    """Nothing to archive means nothing moves — otherwise every retro would
    rewrite lines it has no reason to touch."""
    line = "- [x] T2 already short — fine"
    assert at.split_entry(line) == (line, None)


def test_a_long_entry_without_a_split_point_is_reported_not_truncated() -> None:
    """Machine-choosing a cut point inside prose the author wrote as one
    sentence is worse than leaving it for a human: the ceiling still fails the
    build, so it cannot be silently ignored either."""
    line = "- [x] T3 " + "y" * 400  # no em dash anywhere
    kept, archived = at.split_entry(line)
    assert archived is None and kept == line
    assert at.over_ceiling(line)  # ...and the gate still complains


def test_the_id_match_is_exact_so_a_prefix_cannot_steal_the_line() -> None:
    """`MCP1` must never match the `MCP18` entry — archiving the wrong task
    would move a live task's brief out of the queue."""
    text = "\n".join([_entry("MCP18", "eighteen", "x" * 400), _entry("MCP1", "one", "y" * 400)])
    out, moved = at.archive("MCP1", text)
    assert moved
    assert "- [x] MCP1 one" in out
    assert "eighteen—" + "x" * 400 in out  # MCP18 untouched


def test_an_unknown_or_duplicated_id_fails_loudly() -> None:
    """Silently doing nothing would leave the entry in TASKS.md while the retro
    reports success — the gate would then fail on a later, unrelated PR."""
    with pytest.raises(SystemExit):
        at.archive("NOPE", "- [x] T4 something — else")
    dupe = "\n".join([_entry("D1", "a", "x" * 400), _entry("D1", "b", "y" * 400)])
    with pytest.raises(SystemExit):
        at.archive("D1", dupe)


def test_the_ceiling_ignores_unchecked_items() -> None:
    """An UNCHECKED entry is the queue's actual brief — the content the loop
    exists to read. Capping it would delete the task description itself."""
    pending = "- [ ] T5 " + "z" * 500
    assert at.over_ceiling(pending) == []
    assert at.over_ceiling("- [x] T6 " + "z" * 500)


def test_the_gate_and_the_mover_share_one_ceiling() -> None:
    """A gate that re-implements the mover's threshold is a split truth: the
    two drift, and entries land in the gap where the mover leaves them alone
    and the gate rejects them."""
    just_over = "- [x] T7 " + "w" * (at.MAX_TASK_LINE + 1)
    assert at.over_ceiling(just_over), "the gate must reject what exceeds the ceiling"
    at_limit = "- [x] T8 " + "w" * (at.MAX_TASK_LINE - len("- [x] T8 "))
    assert len(at_limit) == at.MAX_TASK_LINE
    assert at.over_ceiling(at_limit) == [], "the boundary itself is allowed"
    assert at.split_entry(at_limit)[1] is None, "...and the mover agrees it stays put"


def test_the_shipped_tasks_file_is_under_the_ceiling() -> None:
    """The real file, not a fixture: this is the regression that would
    otherwise only surface in CI. Read directly (never through the module's
    sandboxed paths) and read-only — this must observe the repo, not touch it."""
    assert at.over_ceiling((_ROOT / "TASKS.md").read_text(encoding="utf-8")) == []


def test_the_shipped_archive_holds_only_real_completed_tasks_once_each() -> None:
    """Two ways the archive silently rots, both observed during this task:

    * a test writing its fixtures into the real file (ids like `T1` that are
      not tasks at all), and
    * a re-run appending a second copy of an id.

    Either means the archive no longer says what it claims. Checking membership
    and uniqueness catches both without a section count that would rot on every
    completed task.
    """
    tasks = (_ROOT / "TASKS.md").read_text(encoding="utf-8")
    archive = (_ROOT / "docs" / "TASKS_ARCHIVE.md").read_text(encoding="utf-8")
    archived = re.findall(r"^## (\S+)", archive, re.MULTILINE)
    completed = {
        m.group(1) for line in tasks.splitlines() if (m := re.match(r"^- \[x\] (\S+)", line))
    }
    assert len(archived) == len(set(archived)), (
        f"duplicated archive sections: {sorted({i for i in archived if archived.count(i) > 1})}"
    )
    orphans = sorted(set(archived) - completed)
    assert not orphans, f"archive sections with no completed task in TASKS.md: {orphans}"


def test_the_archive_is_only_ever_written_atomically() -> None:
    """THE structural invariant (H22, refined over three Codex rounds): the
    archive is mutated ONLY through the atomic tmp+`os.replace` writer. Any
    direct write path re-opens one of two graves — a truncating open ('w')
    erases every prior section (78 died that way in development), and an
    in-place append ('a') can TEAR, leaving a `## <id>` marker without its
    body, which the idempotency check then reads as "done" while TASKS.md is
    shortened and the as-built is lost forever (Codex #121 r3).

    Checked over the AST rather than the source text: a substring scan trips on
    the module's own prose (docstrings name the forbidden calls to explain the
    rule), firing on documentation and tempting whoever hits it into deleting
    the explanation.
    """
    tree = ast.parse((_ROOT / "scripts" / "archive_task.py").read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        # no builtin open() of the archive at all — reads go through read_text,
        # writes through _replace_file's tmp sibling
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and node.args
            and _is_archive(node.args[0])
        ):
            raise AssertionError("open(_ARCHIVE, …) bypasses the atomic writer")
        # no Path-level write/open either
        if isinstance(node, ast.Attribute) and _is_archive(node.value):
            assert node.attr not in {"write_text", "write_bytes", "open", "touch"}, (
                f"_ARCHIVE.{node.attr}() bypasses the atomic writer"
            )


def _is_archive(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "_ARCHIVE"


def test_a_second_archive_appends_instead_of_replacing(tmp_path: Path) -> None:
    """The truncation this task actually suffered, pinned behaviourally.

    `test_the_shipped_archive_holds_only_real_completed_tasks_once_each` catches
    fixture ids leaking in, but a truncating write that uses a REAL id leaves the
    file valid-looking — subset, no duplicates — while every earlier section is
    gone. Only asserting that an EARLIER entry survives a LATER one closes that,
    and it does so on behaviour rather than on the source text the AST test reads.
    """
    at._TASKS.write_text(
        "\n".join([_entry("A1", "first", "x" * 400), _entry("B2", "second", "y" * 400)]),
        encoding="utf-8",
    )
    assert at.main(["A1"]) == 0
    assert at.main(["B2"]) == 0

    archive = at._ARCHIVE.read_text(encoding="utf-8")
    assert "## A1" in archive, "the first entry was destroyed by the second write"
    assert "## B2" in archive
    assert "x" * 400 in archive and "y" * 400 in archive
    # the header seeds once, on creation — not on every append
    assert archive.count("# TASKS as-built archive") == 1


def test_a_reappend_after_a_crashed_write_does_not_duplicate(tmp_path: Path) -> None:
    """Codex #121: the append and the TASKS.md write are two steps. A crash
    between them leaves the section in the archive while the entry still looks
    long — and the naive fix (append unconditionally) then writes a SECOND copy
    on the next run, breaking one-section-per-task. Simulated by appending, then
    calling the full archive() with the still-long line, exactly what a re-run
    sees.
    """
    long_line = _entry("Z9", "title", "z" * 400)
    at._TASKS.write_text(long_line + "\n", encoding="utf-8")

    _, first = at.archive("Z9", long_line)  # step 1 happened; TASKS.md NOT yet written
    assert first
    # re-run: TASKS.md still has the long line (the write never landed)
    text, second = at.archive("Z9", long_line)
    assert second  # it still shortens the line for the caller to commit
    archive = at._ARCHIVE.read_text(encoding="utf-8")
    assert archive.count("\n## Z9\n") == 1, "the crashed-write re-run duplicated the section"
    assert text.count("- [x] Z9 title") == 1


def test_an_em_dash_inside_brackets_is_not_a_split_point() -> None:
    """The bug that cut nine entries mid-clause.

    Titles routinely carry a parenthetical containing its own em dash. Splitting
    on the FIRST dash regardless of depth severs the title, leaving an unclosed
    bracket in TASKS.md and a fragment in the archive that `grep -A40` then
    reports out of context. With no depth-0 marker there is nothing the tool may
    safely cut, so the entry stays whole — and the ceiling still flags it, which
    is what routes it to a human (H1/H3/H13/BA2d-1 were resolved that way).
    """
    for bracketed in (
        "(owner asked — and it matters)",
        "（實證:full-suite — 確定性餓死）",
        "「甲 — 乙」",
    ):
        line = f"- [x] T9 title {bracketed} " + "q" * 400
        kept, archived = at.split_entry(line)
        assert archived is None, f"must not split inside {bracketed}"
        assert kept == line
        assert at.over_ceiling(line), "...and the human still gets told about it"


def test_a_depth_zero_dash_after_a_bracketed_one_is_the_split_point() -> None:
    """Depth tracking must find the RIGHT dash, not merely skip the wrong one."""
    line = "- [x] TA title(甲 — 乙)— as-built " + "r" * 400
    kept, archived = at.split_entry(line)
    assert kept == "- [x] TA title(甲 — 乙)", "the bracketed dash must survive in the title"
    assert archived is not None and archived.startswith("as-built ")


def test_the_as_built_opener_is_the_other_split_convention() -> None:
    """TASKS.md marks as-built two ways; knowing only one silently leaves whole
    entries unsplit, which the ceiling then rejects with no way for the tool to
    fix them. Unlike the em dash the opener is KEPT — it begins the archived
    text — and the `。` before it is consumed, the one place the split is not a
    pure partition.
    """
    line = "- [x] TB title(甲—乙)。(as-built:" + "s" * 400 + ")"
    kept, archived = at.split_entry(line)
    assert kept == "- [x] TB title(甲—乙)", "the 。 separator is consumed with nothing else"
    assert archived is not None
    assert archived.startswith("(as-built:"), "the opener starts the archived body"
    assert archived.endswith(")")


def test_a_dash_inside_a_markdown_link_is_not_a_split_point() -> None:
    """Codex #121 round 2: link titles are square-bracketed — `[Windows — setup](url)`
    — and a dash inside one is part of the title. Without `[]` in the tracked
    pairs the splitter cut mid-link, leaving a truncated title in TASKS.md and
    the rest of the link in the archive: a lossy split of exactly the kind the
    depth tracking exists to prevent.
    """
    line = "- [x] TC docs pass [Windows — setup](docs/win.md) guide — as-built " + "u" * 400
    kept, archived = at.split_entry(line)
    assert kept == "- [x] TC docs pass [Windows — setup](docs/win.md) guide"
    assert archived is not None and archived.startswith("as-built ")

    # and with NO depth-0 dash at all, the link alone must not invite a cut
    linked_only = "- [x] TD see [Windows — setup](docs/win.md) " + "v" * 400
    kept2, archived2 = at.split_entry(linked_only)
    assert archived2 is None and kept2 == linked_only


def test_a_failed_swap_leaves_the_queue_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex #121 round 2: `write_text` truncates TASKS.md in place, so a crash
    mid-write destroys the QUEUE ITSELF — and since the archive marker already
    exists by then, the rerun sees the append as done and reconstructs nothing.
    The atomic temp+replace makes the failure mode "old queue, complete" instead
    of "new queue, partial". Simulated by failing the replace step: the original
    file must be byte-identical afterwards.
    """
    original = _entry("W1", "title", "w" * 400) + "\n"
    at._TASKS.write_text(original, encoding="utf-8")

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated crash at the swap")

    monkeypatch.setattr(at.os, "replace", boom)
    with pytest.raises(OSError):
        at.main(["W1"])
    assert at._TASKS.read_text(encoding="utf-8") == original, (
        "a failed swap must leave the queue untouched — partial writes destroy it"
    )


def test_a_dash_inside_an_inline_code_span_is_not_a_split_point() -> None:
    """Codex #121 round 3: `` `foo — bar` `` — a dash inside inline code is
    code, not the separator. Splitting there ships an unterminated backtick to
    TASKS.md and the rest of the title to the archive. Brackets inside code
    must not skew the depth either: code is exactly where a lone `(` or
    `dict["k"]` is legal, and a phantom depth would hide the REAL separator
    that follows.
    """
    line = "- [x] TE run `foo — bar` then `x[0](` cleanup — as-built " + "m" * 400
    kept, archived = at.split_entry(line)
    assert kept == "- [x] TE run `foo — bar` then `x[0](` cleanup"
    assert archived is not None and archived.startswith("as-built ")

    # only-dash-inside-code: nothing to cut, entry stays whole, ceiling flags it
    code_only = "- [x] TF see `a — b` " + "n" * 400
    kept2, archived2 = at.split_entry(code_only)
    assert archived2 is None and kept2 == code_only
    assert at.over_ceiling(code_only)


def test_a_crash_during_the_archive_swap_cannot_tear_a_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #121 round 3: with an in-place append, a crash could land the
    `## <id>` marker without its body; the next run's membership check then
    reads the torn section as done and shortens TASKS.md — silently discarding
    the unwritten as-built forever. With the atomic swap the only states are
    old-and-complete / new-and-complete, so the marker can never exist without
    its body. Simulated by failing the swap: the archive must be byte-identical
    (not torn), and the RE-RUN must then complete the archival normally.
    """
    at._TASKS.write_text(_entry("V1", "first", "p" * 400) + "\n", encoding="utf-8")
    assert at.main(["V1"]) == 0  # seed a healthy archive
    healthy = at._ARCHIVE.read_text(encoding="utf-8")

    at._TASKS.write_text(
        at._TASKS.read_text(encoding="utf-8") + _entry("V2", "second", "q" * 400) + "\n",
        encoding="utf-8",
    )

    real_replace = at.os.replace

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated crash at the archive swap")

    monkeypatch.setattr(at.os, "replace", boom)
    with pytest.raises(OSError):
        at.main(["V2"])
    monkeypatch.setattr(at.os, "replace", real_replace)

    assert at._ARCHIVE.read_text(encoding="utf-8") == healthy, (
        "a failed swap must leave the archive exactly as it was — no torn marker"
    )
    # the marker never landed, so the re-run archives V2 for real
    assert at.main(["V2"]) == 0
    final = at._ARCHIVE.read_text(encoding="utf-8")
    assert final.startswith(healthy), "old content must be a strict prefix (append-only)"
    assert "\n## V2\n" in final and "q" * 400 in final


def test_a_changed_narrative_for_an_archived_id_stops_loudly() -> None:
    """Codex #121 round 4: the marker alone is not proof of completeness. If an
    entry acquires a NEW narrative after its id was archived — a later edit, or
    an edit between a crashed run and its re-run — a marker-only check would
    declare it done, shorten the queue line, and the new text would vanish with
    only the stale archived body surviving. Same body must stay a silent no-op
    (that IS the crash-recovery path); a different body must stop loudly and
    leave the queue untouched so a human reconciles the two.
    """
    original = _entry("Y7", "title", "old narrative " + "a" * 400)
    at._TASKS.write_text(original + "\n", encoding="utf-8")
    assert at.main(["Y7"]) == 0
    archived = at._ARCHIVE.read_text(encoding="utf-8")

    # the entry later reappears with a DIFFERENT long narrative
    rewritten = _entry("Y7", "title", "NEW narrative " + "b" * 400)
    at._TASKS.write_text(rewritten + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="DIFFERENT content"):
        at.main(["Y7"])
    assert at._TASKS.read_text(encoding="utf-8") == rewritten + "\n", (
        "the queue must be untouched — shortening it would discard the new narrative"
    )
    assert at._ARCHIVE.read_text(encoding="utf-8") == archived, "archive must be untouched too"

    # and the identical body remains a silent, successful no-op (crash recovery)
    at._TASKS.write_text(original + "\n", encoding="utf-8")
    assert at.main(["Y7"]) == 0


def test_a_multi_backtick_span_is_one_delimiter_not_two() -> None:
    """Codex #121 round 5: Markdown delimits a code span with a RUN of
    backticks closed by a run of the same length — ``x`` opens with two. The
    per-char toggle opened and instantly closed on the first pair, exposing
    the span's dash as a depth-zero separator and cutting TASKS.md mid-span.
    """
    line = "- [x] TG choose ``foo — bar`` wisely — as-built " + "k" * 400
    kept, archived = at.split_entry(line)
    assert kept == "- [x] TG choose ``foo — bar`` wisely"
    assert archived is not None and archived.startswith("as-built ")

    # an only-dash-inside-``…`` line has no separator at all: stays whole
    span_only = "- [x] TH about ``a — b`` " + "j" * 400
    kept2, archived2 = at.split_entry(span_only)
    assert archived2 is None and kept2 == span_only
    assert at.over_ceiling(span_only)
