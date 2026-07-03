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
      { git show "$ref:$f" 2>/dev/null || true; } \
        | { grep -E '^[[:space:]]*version:[[:space:]]*' || true; } | head -n1 \
        | sed -E 's/^[[:space:]]*version:[[:space:]]*//; s/["'"'"']//g'
      ;;
  esac
}
while IFS= read -r f; do
  [ -z "$f" ] && continue
  git cat-file -e "$base:$f" 2>/dev/null || continue # newly added = the freeze itself, exempt
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
$(git diff --name-only "$base"...HEAD | grep '^contracts/' || true)
EOF

# --- TASKS.md checkoff: a task/<id> PR must check off exactly its own item ---
case "$BRANCH" in
  task/*)
    id="${BRANCH#task/}"
    # capture first: grep -q would SIGPIPE the diff under pipefail on early match
    tasks_diff="$(git diff "$base"...HEAD -- TASKS.md)"
    if ! printf '%s' "$tasks_diff" | grep -qE "^\+- \[x\] $id[[:space:]]"; then
      echo "::error file=TASKS.md::branch $BRANCH must check off item '$id' in TASKS.md (the checkoff rides in the task PR)"
      fail=1
    fi
    ;;
  *)
    echo "non task/* branch — checkoff lint skipped"
    ;;
esac

exit "$fail"
