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

# --- DR-002: changing a frozen contract requires a version bump in the same diff ---
changed_contracts="$(git diff --name-only "$base"...HEAD | grep '^contracts/' || true)"
for f in $changed_contracts; do
  if git cat-file -e "$base:$f" 2>/dev/null; then # pre-existing => a change to a frozen artifact
    if ! git diff "$base"...HEAD -- "$f" | grep -qE '^[+-].*("schema_version"|version:)'; then
      echo "::error file=$f::DR-002: $f changed without a schema_version/version bump in the same diff"
      fail=1
    fi
  fi
done

# --- TASKS.md checkoff: a task/<id> PR must check off exactly its own item ---
case "$BRANCH" in
  task/*)
    id="${BRANCH#task/}"
    if ! git diff "$base"...HEAD -- TASKS.md | grep -qE "^\+- \[x\] $id[[:space:]]"; then
      echo "::error file=TASKS.md::branch $BRANCH must check off item '$id' in TASKS.md (the checkoff rides in the task PR)"
      fail=1
    fi
    ;;
  *)
    echo "non task/* branch — checkoff lint skipped"
    ;;
esac

exit "$fail"
