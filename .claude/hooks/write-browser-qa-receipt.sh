#!/usr/bin/env bash
# write-browser-qa-receipt.sh <evidence-file> [<evidence-file> ...]
#
# Called after the FE browser pass (LOOP.md step 4, FE-only): the agent drove
# a real Chrome (Claude in Chrome) through the task's UI flows and captured
# screenshot/GIF evidence. Stamps a receipt binding that pass to the exact
# content state — the same throwaway-index tree hash as
# write-review-receipt.sh — which the push gate (require-push-gates.sh)
# requires on task/FE* branches, so "edited after the pass" is mechanically
# unpushable (H10). A stamp WITHOUT existing, non-empty evidence files is
# refused: a claim is not a pass. The self-stamp is weaker than a
# reviewer-stamp (H5), so the same artifacts go into the PR body where the
# owner and Codex can audit them.
#
# Receipts are content-addressed in their OWN namespace:
# .claude/receipts/browser-qa-<tree> — a review receipt can never satisfy the
# browser gate, nor vice versa.
set -e
[ "$#" -ge 1 ] || { echo "usage: write-browser-qa-receipt.sh <evidence-file> [...]" >&2; exit 1; }
cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
top="$(git rev-parse --show-toplevel)"
for f in "$@"; do
  [ -s "$f" ] || { echo "evidence file missing or empty: $f — the browser pass needs real artifacts" >&2; exit 1; }
  # evidence must NEVER enter the content trees: the receipt snapshot and
  # the push gate's worktree==HEAD check both assume it (an unignored
  # in-repo artifact would deadlock the FE gate — Codex #64 R13). Inside
  # the repo it must be gitignored; outside (e.g. the session scratchpad)
  # is always safe.
  # compare git TOPLEVELS, not string prefixes — Git Bash mixes /c/... and
  # C:/... path forms, and only git normalizes both sides consistently
  ev_top="$(git -C "$(dirname "$f")" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ "$ev_top" = "$top" ]; then
    git check-ignore -q -- "$f" ||
      { echo "evidence inside the repo must be gitignored (or store it outside, e.g. the session scratchpad): $f — an unignored artifact enters the receipt trees and deadlocks the worktree==HEAD gate" >&2; exit 1; }
  fi
done

# git refuses a zero-byte index file, so reserve a name and DELETE it — git then
# creates a fresh index at that path (same idiom as write-review-receipt.sh).
tmp_index="$(mktemp)"
[ -n "$tmp_index" ] || { echo "mktemp failed — refusing to touch the real index" >&2; exit 1; }
rm -f "$tmp_index"
trap 'rm -f "$tmp_index"' EXIT
GIT_INDEX_FILE="$tmp_index" git add -A >/dev/null 2>&1
tree="$(GIT_INDEX_FILE="$tmp_index" git write-tree)"

mkdir -p .claude/receipts
# line 1 = header; then ONE evidence path per line (paths may contain spaces).
# The push gate re-validates each path is still a non-empty file at push time:
# evidence is untracked/ignored, so it never enters the bound tree — without
# the liveness re-check, deleting/truncating it after the stamp would go
# unnoticed (bind-time stamp ≠ invariant; Codex #64 R2, class 10).
{
  printf '%s browser-qa %s %s\n' "$tree" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$#"
  printf '%s\n' "$@"
} > ".claude/receipts/browser-qa-$tree"
echo "browser-qa receipt stamped: tree=$tree evidence=$#"
