#!/usr/bin/env bash
# write-review-receipt.sh <code-reviewer|doc-reviewer>
#
# Called by a reviewer subagent on VERDICT: PASS (and only then). Stamps a
# receipt binding the PASS to the exact content state that was reviewed: a git
# tree hash computed over tracked AND untracked (non-ignored) files via a
# throwaway index — the real index is untouched. The push gate
# (require-push-gates.sh) recomputes the same hash and refuses to push anything
# that no longer matches, so "edited after review" is mechanically unpushable.
#
# Receipts are content-addressed (H5): one file per reviewed tree,
# .claude/receipts/<tree>, so parallel branches (task/* + docs/*) never
# clobber each other's stamps. Identical content = identical review subject,
# so a receipt legitimately survives switching away and back.
set -e
reviewer="${1:?usage: write-review-receipt.sh <code-reviewer|doc-reviewer>}"
case "$reviewer" in code-reviewer|doc-reviewer) : ;; *) echo "unknown reviewer '$reviewer'" >&2; exit 1 ;; esac
cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

# git refuses a zero-byte index file, so reserve a name and DELETE it — git then
# creates a fresh index at that path (mktemp -u would be racy-in-theory; this isn't).
tmp_index="$(mktemp)"
[ -n "$tmp_index" ] || { echo "mktemp failed — refusing to touch the real index" >&2; exit 1; }
rm -f "$tmp_index"
trap 'rm -f "$tmp_index"' EXIT
GIT_INDEX_FILE="$tmp_index" git add -A >/dev/null 2>&1
tree="$(GIT_INDEX_FILE="$tmp_index" git write-tree)"

mkdir -p .claude/receipts
printf '%s %s %s\n' "$tree" "$reviewer" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > ".claude/receipts/$tree"
echo "review receipt stamped: tree=$tree by=$reviewer"
