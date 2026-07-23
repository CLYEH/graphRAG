#!/usr/bin/env bash
# CI governance: deterministic (CPU, server-side) enforcement of loop-protocol
# invariants that were previously instruction-only. Runs on pull_request with a
# full-depth checkout; expects BASE (base ref) and BRANCH (head ref) env vars.
set -euo pipefail
: "${BASE:?BASE (base ref) required}"
: "${BRANCH:?BRANCH (head ref) required}"
git fetch -q origin "$BASE"
base="origin/$BASE"
fail=0

# --- DR-002: changing a frozen contract requires an actual version-value bump ---
# Compare the extracted version VALUE between base and HEAD (a token-grep on the
# diff misses the mcp schema, whose bump only changes a `"const": "1.x"` line).
version_of() { # version_of <ref> <file> — emits empty (never aborts) when the file/field is missing
  local ref="$1" f="$2"
  case "$f" in
    *.json)
      { git show "$ref:$f" 2>/dev/null || true; } \
        | jq -r '(.properties.schema_version.const // .info.version // .version // empty)' 2>/dev/null || true
      ;;
    *.yaml | *.yml)
      # strip trailing comments/whitespace too: comparing raw line text would let a
      # cosmetic edit of the version line fake a "bump" without changing the value
      { git show "$ref:$f" 2>/dev/null || true; } \
        | { grep -E '^[[:space:]]*version:[[:space:]]*' || true; } | head -n1 \
        | sed -E 's/^[[:space:]]*version:[[:space:]]*//; s/[[:space:]]+#.*$//; s/["'"'"']//g; s/[[:space:]]+$//'
      ;;
  esac
}
while IFS= read -r f; do
  [ -z "$f" ] && continue
  git cat-file -e "$base:$f" 2>/dev/null || continue # newly added = the freeze itself, exempt
  if ! git cat-file -e "HEAD:$f" 2>/dev/null; then
    echo "::error file=$f::DR-002: frozen contract $f was deleted/renamed — contracts are frozen artifacts; restore it or land a versioned replacement recorded in DESIGN §26"
    fail=1
    continue
  fi
  base_v="$(version_of "$base" "$f")"
  head_v="$(version_of HEAD "$f")"
  if [ -z "$head_v" ]; then
    echo "::error file=$f::DR-002: cannot locate a version field in $f — add one or teach scripts/governance-check.sh its format"
    fail=1
  elif [ "$base_v" = "$head_v" ]; then
    echo "::error file=$f::DR-002: $f changed but its version stayed '$head_v' — bump schema_version/version in the same diff"
    fail=1
  fi
done <<EOF
$(git diff --name-only --no-renames "$base"...HEAD | grep '^contracts/' || true)
EOF

# --- TASKS.md checkoff: a task/<id> PR must check off exactly its own item ---
case "$BRANCH" in
  task/*)
    id="${BRANCH#task/}"
    # Compare checked-off STATE, not diff lines (H22). A `+- [x] X` line only
    # means "X was checked off here" if X was NOT already checked in base —
    # otherwise any reformat of a settled entry (which is exactly what the
    # archive step does at retro) reads as smuggling in dozens of foreign
    # checkoffs. The invariant this gate protects is about state transitions,
    # so it has to be evaluated on state.
    checked_ids() { # checked_ids <ref> — the ids marked [x] in that ref's TASKS.md
      { git show "$1:TASKS.md" 2>/dev/null || true; } \
        | { grep -oE '^- \[x\] [^[:space:]]+' || true; } \
        | sed -E 's/^- \[x\] //' | sort -u
    }
    newly_checked="$(comm -13 <(checked_ids "$base") <(checked_ids HEAD))"
    if ! printf '%s\n' "$newly_checked" | grep -qxF "$id"; then
      echo "::error file=TASKS.md::branch $BRANCH must check off item '$id' in TASKS.md (the checkoff rides in the task PR)"
      fail=1
    fi
    # "exactly its own item": no smuggled checkoffs of other items
    extra="$(printf '%s\n' "$newly_checked" | { grep -vxF "$id" || true; } | { grep -v '^$' || true; })"
    if [ -n "$extra" ]; then
      echo "::error file=TASKS.md::branch $BRANCH adds checkoffs other than its own item '$id': $(printf '%s' "$extra" | tr '\n' ' ')"
      fail=1
    fi
    ;;
  *)
    echo "non task/* branch — checkoff lint skipped"
    ;;
esac

# --- H22: TASKS.md checked-off lines stay one-line summaries ---
# The loop reads TASKS.md on EVERY iteration to pick the next task, so an
# as-built narrative left inline on a `- [x]` line is a recurring context tax on
# work it cannot inform. scripts/archive_task.py moves those into
# docs/TASKS_ARCHIVE.md at retro; this gate is what stops the file drifting back.
# The ceiling AND the check live in that one module on purpose — a gate that
# re-implements the mover's threshold is a split truth waiting to disagree.
# portable interpreter discovery: the CI ubuntu image has `python3`, but the
# Git-for-Windows environment this repo's governance tests explicitly support
# (see tests/test_governance_check.py::_find_bash) commonly ships only
# `python`/`py` — hardcoding either name fails the gate on a machine whose
# Python is perfectly present (Codex #121). First match wins.
py=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1; then py="$cand"; break; fi
done
if [ -z "$py" ]; then
  echo "::error::governance: no python interpreter found (looked for python3/python/py)"
  fail=1
else
  "$py" "$(dirname "$0")/archive_task.py" --check || fail=1
fi

# --- H20d: memory stale-claims (warning annotations only — NEVER gates) ---
# `|| true` is deliberate fail-open: this tier is advisory (owner decision,
# TASKS H20d), so a bug in the warning script must not block a PR.
bash "$(dirname "$0")/memory-stale-claims.sh" TASKS.md .claude/memory || true

exit "$fail"
