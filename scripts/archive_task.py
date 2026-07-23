#!/usr/bin/env python
"""Move a completed task's as-built narrative out of TASKS.md into the archive.

WHY THIS EXISTS (H22): the loop reads `TASKS.md` on every iteration to pick the
next task, but the file is dominated by the as-built narratives of tasks that
are already DONE — prose that mattered at retro time and is pure noise when
choosing what to do next. Left unchecked it is a recurring, growing context tax
on work it cannot possibly inform.

THE INVARIANT: **the archive's size must never become a cost of the retro.**
The tax this exists to remove is the LOOP pulling completed-task prose into its
working context every iteration; the archive must not just move that tax to a
different file. So:

* The archive is **never surfaced to the loop/agent** — entries are appended in
  chronological order and looked up by `grep -A40 '^## <TASK-ID>'`, never read
  whole into context; nothing sorts, dedupes, or reformats it (each of those
  would load the entire file).
* Writes are **append-only** ('a') — a truncating open would erase every prior
  section (a mis-flipped test did exactly that to 78 of them in development).

`_append_archive` DOES read the archive for one thing: a bounded membership
check that keeps the append idempotent (a crash between the append and the
`TASKS.md` write must not let a re-run duplicate the section — Codex #121).
That read lives inside a once-per-task retro subprocess and never reaches the
agent's context, so it does not reintroduce the tax; it is O(size), not a sort.
`TASKS.md` itself is read and rewritten every run — that is the whole job.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

#: Split point between "what the task was" (stays in TASKS.md) and "how it went"
#: (moves to the archive). The em dash is the convention every long entry in the
#: file already uses; the FIRST one wins because as-built prose contains more.
_DASH = "—"

#: A line at or under this stays put — there is nothing worth archiving, and the
#: governance lint uses the SAME number so the two can never disagree about what
#: "short enough" means (a split gate is a split truth).
MAX_TASK_LINE = 300

#: Resolved against the CURRENT WORKING DIRECTORY, not this file's location:
#: the gate must judge the repo it is being run in (CI checks out a work tree
#: and runs from its root, and the governance tests run it inside throwaway
#: repos). Anchoring to `__file__` would make every one of those silently
#: inspect THIS checkout's TASKS.md instead — a gate that always passes.
_TASKS = Path("TASKS.md")
_ARCHIVE = Path("docs") / "TASKS_ARCHIVE.md"

_ARCHIVE_HEADER = """# TASKS as-built archive

Append-only. One `## <TASK-ID>` section per completed task, in the order they
were archived — **not** sorted, not grouped. Written by `scripts/archive_task.py`
during the step-8 retro (see `docs/LOOP.md`).

Look things up with `grep -A40 '^## <TASK-ID>' docs/TASKS_ARCHIVE.md` — never by
reading the whole file, which is exactly the cost this split exists to avoid.
"""


def _emit(message: str) -> None:
    """Write a CI annotation as UTF-8, explicitly.

    `print` encodes with the locale codepage, which on Windows is not UTF-8 —
    the excerpts here are Chinese, so the annotation arrives as mojibake and
    anything parsing this output (the governance tests do) fails to decode it
    at all. The gate's verdict is in the exit code either way, but an
    unreadable reason is a gate nobody can act on.
    """
    sys.stdout.buffer.write((message + "\n").encode("utf-8"))
    sys.stdout.flush()


def _task_line_re(task_id: str) -> re.Pattern[str]:
    # the id is followed by whitespace so `MCP1` cannot match the `MCP18` line
    return re.compile(rf"^- \[x\] {re.escape(task_id)}(?=\s)", re.MULTILINE)


#: Bracket pairs that may enclose an em dash. ASCII and full-width both: the
#: file is bilingual and the CJK forms are the common case. Square brackets are
#: in the set because Markdown link titles are bracketed — `[Windows — setup](…)`
#: — and a dash inside one is part of the title, not the separator (Codex #121).
_BRACKETS = {"(": ")", "（": "）", "「": "」", "『": "』", "[": "]", "【": "】"}
_CLOSERS = {close: open_ for open_, close in _BRACKETS.items()}


#: The file marks the start of an as-built section two ways, and the splitter
#: has to know both or it silently leaves whole entries unsplit (which the
#: ceiling then rejects, stalling the retro on an entry nobody can fix by
#: running the tool again).
_AS_BUILT_OPENERS = ("(as-built", "（as-built", "(as‑built", "（as‑built")


def _split_index(line: str) -> int:
    """Index where the as-built section starts, or -1 if the line has none.

    Only a marker at bracket depth ZERO counts. Titles routinely carry a
    parenthetical — `(owner 2026-07-20 詢問「…」— …)`, `(#112 期間…實證:…)` —
    and splitting on the first dash *inside* one severs the title mid-clause,
    leaving an unclosed bracket in TASKS.md and a fragment in the archive that
    `grep -A40 '^## <ID>'` then reports out of context. Nine entries were cut
    that way before this was depth-aware.
    """
    depth = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        # Inline code spans are opaque (Codex #121 r3/r5): a dash inside
        # `` `foo — bar` `` is code, and brackets inside code (`dict["k"]`, a
        # lone `(`…) must not skew the depth — code is exactly where unbalanced
        # brackets are legal. Markdown delimits a span with a RUN of backticks
        # closed by a run of the SAME length (`` x `` uses two), so a per-char
        # toggle would open and instantly close on a multi-backtick delimiter
        # and expose the span's contents (r5). An unclosed span makes the REST
        # of the line opaque: no split is found, the entry stays whole, and
        # the ceiling routes it to a human — fail-safe, same as no-split-point.
        if ch == "`":
            run_start = i
            while i < n and line[i] == "`":
                i += 1
            fence = line[run_start:i]
            close = line.find(fence, i)
            # an equal-length run that is part of a LONGER run does not close
            # (CommonMark); scan forward until an exact-length run or give up
            while close >= 0 and close + len(fence) < n and line[close + len(fence)] == "`":
                after = close
                while after < n and line[after] == "`":
                    after += 1
                close = line.find(fence, after)
            if close < 0:
                return -1  # unclosed span — the rest of the line is opaque
            i = close + len(fence)
            continue
        if depth == 0 and line.startswith(_AS_BUILT_OPENERS, i):
            return i - 1 if line[i - 1 : i] == "。" else i
        if ch in _BRACKETS:
            depth += 1
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
        elif ch == _DASH and depth == 0:
            return i
        i += 1
    return -1


def split_entry(line: str) -> tuple[str, str | None]:
    """``(kept, archived)`` for one TASKS.md line; ``archived`` is None when the
    line is already short enough to leave alone."""
    if len(line) <= MAX_TASK_LINE:
        return line, None
    cut = _split_index(line)
    # an as-built opener is kept (it starts the archived text); an em dash is
    # the separator itself and is consumed
    tail_from = cut if line.startswith(_AS_BUILT_OPENERS, cut) else cut + 1
    if cut < 0 or not line[tail_from:].strip():
        # No split point at depth zero: the line is long but has no as-built
        # section the author marked off. Report it rather than truncating — a
        # machine-chosen cut point inside prose written as one sentence is
        # worse than a human rewriting it, and the ceiling still fails the
        # build so it cannot be quietly ignored either.
        return line, None
    return line[:cut].rstrip(), line[tail_from:].strip()


def _replace_file(path: Path, text: str) -> None:
    """Replace ``path`` atomically: temp sibling + ``os.replace``.

    In-place writes (``Path.write_text``, ``open(..., 'a')``) can tear — a
    crash mid-write leaves the file partial, and BOTH files here are
    unrecoverable when torn: a partial ``TASKS.md`` destroys the queue itself,
    and a partial archive section reads as "already archived" to the
    idempotency check, so the next run shortens ``TASKS.md`` and the unwritten
    remainder of the as-built is lost forever (Codex #121 rounds 2–3). With an
    atomic swap the only two observable states are old-and-complete or
    new-and-complete. Same-directory sibling, so no cross-device rename; the
    temp is best-effort removed when the swap itself fails.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)  # don't leave debris next to the queue
        raise


def _section_marker(task_id: str) -> str:
    # the trailing newline is load-bearing: it stops `## MCP1` from matching
    # inside `## MCP18`, the same prefix hazard `_task_line_re` guards
    return f"\n## {task_id}\n"


def _append_archive(task_id: str, body: str) -> None:
    """Add one section to the archive — idempotent, atomic, content-append-only.

    Three properties, each answering a failure that actually occurred or was
    review-identified on this task:

    * **Idempotent** — a crash after this lands but before the caller commits
      the shortened ``TASKS.md`` leaves the section present while the entry
      still looks unarchived; the membership check makes the re-run a no-op
      instead of a duplicate (Codex #121 r1).
    * **Atomic** — an in-place ``'a'`` append can TEAR: the ``## <id>`` marker
      reaches disk but the body doesn't, and the next run's membership check
      then reads the torn section as "done" and shortens ``TASKS.md`` anyway —
      permanently discarding the unwritten as-built (Codex #121 r3). The whole
      new content goes through the same tmp+``os.replace`` swap as the queue,
      so a torn archive is unrepresentable.
    * **Content-append-only** — the new content is exactly the old content
      plus one section. Nothing sorts, dedupes, or reformats; a change that
      shrinks the file is the truncation class that destroyed 78 sections in
      development, and the behavioural tests pin old-content-is-a-prefix.

    The full read here is fine under the module invariant: it happens once per
    archived task inside a retro subprocess and never reaches the loop's
    context — archive size still costs the retro nothing it can feel.
    """
    _ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    old = _ARCHIVE.read_text(encoding="utf-8") if _ARCHIVE.exists() else ""
    marker = _section_marker(task_id)
    at = old.find(marker)
    if at >= 0:
        # A marker alone is NOT proof of completeness (Codex #121 r4): if the
        # entry acquired a NEW narrative after this id was archived — a later
        # edit, or an edit between a crashed run and its re-run — "already
        # done" would let the caller shorten the queue line and silently
        # discard the new text, keeping only the stale body. Same body →
        # idempotent no-op; different body → loud stop, queue untouched, a
        # human decides which narrative wins.
        start = at + len(marker)
        next_section = old.find("\n## ", start)
        stored = old[start : next_section if next_section >= 0 else len(old)].strip()
        if stored != body.strip():
            raise SystemExit(
                f"archive_task: section '{task_id}' already exists in {_ARCHIVE} with "
                "DIFFERENT content — refusing to shorten the TASKS.md entry, which "
                "would silently discard the newer narrative. Reconcile the two "
                f"bodies by hand (grep -A40 '^## {task_id}' {_ARCHIVE}), then rerun."
            )
        return  # identical body — an interrupted earlier run already archived it
    seed = old or _ARCHIVE_HEADER
    _replace_file(_ARCHIVE, f"{seed}{marker}\n{body}\n")


def archive(task_id: str, tasks_text: str) -> tuple[str, bool]:
    """Return ``(new_tasks_text, archived?)``. Appends to the archive as a side
    effect (idempotently — see ``_append_archive``); the caller commits the
    returned text to ``TASKS.md`` to close the transaction."""
    pattern = _task_line_re(task_id)
    lines = tasks_text.split("\n")
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]
    if not hits:
        raise SystemExit(f"archive_task: no completed `- [x] {task_id}` line in TASKS.md")
    if len(hits) > 1:
        raise SystemExit(f"archive_task: {task_id} matches {len(hits)} lines — ids must be unique")
    kept, archived = split_entry(lines[hits[0]])
    if archived is None:
        return tasks_text, False
    _append_archive(task_id, archived)
    lines[hits[0]] = kept
    return "\n".join(lines), True


def _ids_needing_archive(tasks_text: str) -> list[str]:
    out = []
    for line in tasks_text.split("\n"):
        m = re.match(r"^- \[x\] (\S+)", line)
        if m and split_entry(line)[1] is not None:
            out.append(m.group(1))
    return out


def over_ceiling(tasks_text: str) -> list[tuple[int, int, str]]:
    """``(line_no, length, excerpt)`` for every CHECKED entry above the ceiling.

    Checked lines only: an UNCHECKED item is the queue's actual brief and must
    stay whole — that is the content the loop exists to read.
    """
    out = []
    for i, line in enumerate(tasks_text.splitlines(), start=1):
        if line.startswith("- [x] ") and len(line) > MAX_TASK_LINE:
            out.append((i, len(line), line[:60]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("task_id", nargs="?", help="the completed task's id, e.g. MCP2")
    ap.add_argument("--all", action="store_true", help="one-time migration of every long entry")
    ap.add_argument(
        "--check",
        action="store_true",
        help="gate mode: report checked entries over the ceiling, exit 1 (used by CI)",
    )
    args = ap.parse_args(argv)
    if args.check:
        if args.task_id or args.all:
            ap.error("--check takes no other arguments")
        offenders = over_ceiling(_TASKS.read_text(encoding="utf-8"))
        for line_no, length, excerpt in offenders:
            _emit(
                f"::error file=TASKS.md,line={line_no}::H22: completed entry is {length} "
                f"chars (ceiling {MAX_TASK_LINE}) — run "
                f"'uv run python scripts/archive_task.py <TASK-ID>' to move its as-built "
                f"into docs/TASKS_ARCHIVE.md  [{excerpt}…]"
            )
        return 1 if offenders else 0
    if bool(args.task_id) == args.all:
        ap.error("give exactly one of <task_id> or --all")

    text = _TASKS.read_text(encoding="utf-8")
    ids = _ids_needing_archive(text) if args.all else [args.task_id]

    done = 0
    for task_id in ids:
        text, moved = archive(task_id, text)
        if moved:
            # commit TASKS.md per task, not once at the end: each archival is
            # then its own transaction (append + this write), so an interrupted
            # --all leaves at most the in-flight task to redo, and the append's
            # idempotency makes even that redo a no-op (Codex #121)
            _replace_file(_TASKS, text)
            done += 1
        elif not args.all:
            _emit(f"archive_task: {task_id} is already short enough — nothing moved")
    print(f"archive_task: archived {done} entr{'y' if done == 1 else 'ies'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
