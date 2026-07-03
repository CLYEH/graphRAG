#!/usr/bin/env bash
# PreToolUse gate on `git push`: CPU-verifies the loop's feed-forward claims at
# the transition point instead of trusting the agent's narrative.
#
#   Task-branch push (full lane):
#     - requires a code-reviewer PASS receipt whose tree hash matches the
#       current content (see write-review-receipt.sh) — editing anything after
#       the review makes the push mechanically impossible;
#     - re-runs `uv run poe check` itself (and `poe web-check` when web/ files
#       are outgoing) — "gates were green" is re-executed, not believed.
#   Direct push to main (doc-only fast lane, LOOP.md):
#     - every outgoing file must be *.md, else denied (use a PR);
#     - requires a doc-reviewer (or code-reviewer) receipt matching the content.
#       CI-green on the SHA is enforced server-side by branch protection.
#
# Like require-codex-approval.sh this is local, honest-agent enforcement.
# Fail-closed. Wired via .claude/settings.json (matcher Bash|PowerShell).
set -o pipefail
deny() { printf 'push-gate: %s\n' "$1" >&2; exit 2; }

payload="$(cat)"
printf '%s' "$payload" | grep -Eq 'git[[:space:]]+push' || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || deny "cannot cd to the project dir -> blocked."

git fetch -q origin main 2>/dev/null
current="$(git branch --show-current)"
[ -z "$current" ] && deny "detached HEAD — push from a branch."

# lane: an explicit ':main' refspec or pushing while on main = doc-only fast lane
if printf '%s' "$payload" | grep -Eq ":main([[:space:]\"']|$)" || [ "$current" = "main" ]; then
  lane=doc
else
  lane=task
fi

outgoing="$(git diff --name-only origin/main...HEAD 2>/dev/null)"
if [ -z "$outgoing" ] && [ -z "$(git status --porcelain)" ]; then
  exit 0 # nothing new — plain sync push
fi

snapshot_tree() {
  local tmp tree
  tmp="$(mktemp)"
  GIT_INDEX_FILE="$tmp" git add -A >/dev/null 2>&1
  tree="$(GIT_INDEX_FILE="$tmp" git write-tree)"
  rm -f "$tmp"
  printf '%s' "$tree"
}

require_receipt() {
  [ -f .claude/receipts/review ] || deny "no review receipt — a reviewer subagent must PASS this content first (it stamps .claude/receipts/review)."
  local tree reviewer rest now
  read -r tree reviewer rest < .claude/receipts/review
  now="$(snapshot_tree)"
  [ "$tree" = "$now" ] || deny "review receipt is stale — content changed since $reviewer's PASS. Re-run the reviewer."
  case "$lane:$reviewer" in
    task:code-reviewer | doc:doc-reviewer | doc:code-reviewer) : ;;
    *) deny "receipt from '$reviewer' does not satisfy the $lane lane (task lane needs code-reviewer)." ;;
  esac
}

if [ "$lane" = doc ]; then
  nonmd="$(printf '%s\n' "$outgoing" | grep -v '\.md$' || true)"
  [ -n "$nonmd" ] && deny "direct-to-main is the DOC-ONLY lane; non-.md outgoing: $(printf '%s' "$nonmd" | tr '\n' ' ')— use a task branch + PR."
  require_receipt
else
  require_receipt
  uv run poe check >/dev/null 2>&1 || deny "backend gates are red — run 'uv run poe check', fix, re-review, then push."
  if printf '%s\n' "$outgoing" | grep -q '^web/'; then
    uv run poe web-check >/dev/null 2>&1 || deny "frontend gates are red — run 'uv run poe web-check', fix, re-review, then push."
  fi
fi
exit 0
