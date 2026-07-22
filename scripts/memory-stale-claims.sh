#!/usr/bin/env bash
# H20d (lesson class 2 — entry-point consistency): WARN, never fail, when a
# memory file still claims a task is pending after TASKS.md has checked that
# task off. Stale prose misleads the next session's prep read.
#
# Shape discipline (class 14): this deliberately does NOT parse prose meaning.
# The checked ids are enumerated from TASKS.md itself (the catalog — no hand
# list to forget), the pending markers are a small CLOSED set, and a warning
# needs BOTH on the same line. Anything subtler than that is a human read,
# not a lint.
#
# Perf note: one awk pass per file — a per-id grep inside a per-line loop is
# O(ids × lines) process SPAWNS, which took minutes on Windows git-bash.
#
# Usage: memory-stale-claims.sh <TASKS.md> <memory-dir>
# Always exits 0 — this is a warning annotation, not a gate.
set -euo pipefail
tasks="${1:?TASKS.md path required}"
memdir="${2:?memory dir required}"

# checked task ids, enumerated from TASKS.md itself, as one alternation.
# sed -n…p: a checked line whose id the pattern can't extract (e.g. the
# struck-through ~~H10~~ drop) is SKIPPED — without -n, sed passes the whole
# unmatched line through and the alternation stops being ids at all (caught
# by execution, class 7). The id must CONTAIN A DIGIT — that is the property
# separating real task ids (H21, MCP1, GOV2-fe) from generic checked words
# in the early setup items ("CI"), which would otherwise warn on every
# ordinary use of the word (first live run caught exactly that).
# KNOWN RESIDUAL (gate-2 #116-era review): short id families (P2, C3, H8…)
# are ALSO prose tokens in memory files (Codex priority labels, pipeline
# stage names). Clean today; if a warning ever fires on one of those, the
# resolution is reword-the-clause or add an id exemption here — the lint is
# not broken.
id_alt="$(
  { grep -E '^- \[x\] ' "$tasks" || true; } \
    | sed -nE 's/^- \[x\] ([A-Za-z][A-Za-z0-9-]*[0-9][A-Za-z0-9-]*).*/\1/p' \
    | sort -u | paste -sd'|' -
)"
[ -z "$id_alt" ] && exit 0

# finite pending-marker allowlist (closed set on purpose — see header)
markers='尚餘|尚未|待實作|待做|未立案|還沒|剩下|not yet|remaining'

# co-occurrence is judged per CLAUSE, not per line: a long index/description
# line legitimately says "GOV3-fe 已 merge; …; 尚餘 gap-list FE 片" — the
# pending claim and the finished id live in different clauses (first live
# run caught exactly that). Clause delimiters: ;/；/。/——.
find "$memdir" -name '*.md' | sort | while IFS= read -r f; do
  awk -v ids="$id_alt" -v markers="$markers" -v file="$f" '
    $0 ~ markers {
      m = split($0, segs, /(;|；|。|——)/)
      n = split(ids, arr, "|")
      for (s = 1; s <= m; s++) {
        if (segs[s] !~ markers) continue
        for (i = 1; i <= n; i++) {
          # word boundary by hand: [A-Za-z0-9-] on either side would make it
          # a DIFFERENT id (H2 must not fire on an H20 line)
          if (match(segs[s], "(^|[^A-Za-z0-9-])" arr[i] "([^A-Za-z0-9-]|$)")) {
            printf "::warning file=%s,line=%d::stale-claim? '\''%s'\'' 已在 TASKS.md 勾稽,但此行(同一子句)仍稱其待辦——更新或刪除這段記憶(class 2)\n", file, NR, arr[i]
          }
        }
      }
    }
  ' "$f"
done
exit 0
