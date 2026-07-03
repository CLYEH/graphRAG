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
# engage on `git [flags] push` incl. `git -C <path> push` and `git --git-dir=<p> push`;
# must not engage on e.g. `git log push-fix`
printf '%s' "$payload" | grep -Eq 'git([[:space:]]+-[A-Za-z-]+(=[^[:space:]]+|[[:space:]]+[^[:space:]]+)?)*[[:space:]]+push\b' || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || deny "cannot cd to the project dir -> blocked."

git fetch -q origin main 2>/dev/null
current="$(git branch --show-current)"
[ -z "$current" ] && deny "detached HEAD — push from a branch."

# lane: a ':main' / ':refs/heads/main' refspec, pushing while on main, or a docs/* branch
# (whose whole purpose is the fast lane, incl. its own branch push) = doc-only lane
if printf '%s' "$payload" | grep -Eq ":(refs/heads/)?main([[:space:]\"']|$)" \
  || [ "$current" = "main" ] || [[ "$current" == docs/* ]]; then
  lane=doc
else
  lane=task
fi

# --no-renames: a rename must surface its OLD path too, or `git mv core/x.py docs/x.md`
# would look all-.md and smuggle a code deletion through the doc lane
outgoing="$(git diff --name-only --no-renames origin/main...HEAD 2>/dev/null)"
if [ -z "$outgoing" ] && [ -z "$(git status --porcelain)" ]; then
  exit 0 # nothing new — plain sync push
fi

snapshot_tree() {
  local tmp tree
  tmp="$(mktemp)" && [ -n "$tmp" ] || deny "mktemp failed -> blocked (would touch the real index)."
  rm -f "$tmp" # git refuses a zero-byte index; a fresh path makes it create one
  GIT_INDEX_FILE="$tmp" git add -A >/dev/null 2>&1
  tree="$(GIT_INDEX_FILE="$tmp" git write-tree)"
  rm -f "$tmp"
  [ -n "$tree" ] || deny "snapshot tree computation failed -> blocked."
  printf '%s' "$tree"
}

require_receipt() {
  # receipts are content-addressed (H5): .claude/receipts/<tree>, one per
  # reviewed state — parallel branches don't clobber each other's stamps
  local tree reviewer rest now
  now="$(snapshot_tree)"
  [ -f ".claude/receipts/$now" ] || deny "no review receipt for this content — a reviewer subagent must PASS this exact state (it stamps .claude/receipts/<tree>); anything edited after its PASS needs a re-review."
  read -r tree reviewer rest < ".claude/receipts/$now"
  [ "$tree" = "$now" ] || deny "receipt .claude/receipts/$now is corrupt (names tree '$tree') — re-run the reviewer."
  case "$lane:$reviewer" in
    task:code-reviewer | doc:doc-reviewer | doc:code-reviewer) : ;;
    *) deny "receipt from '$reviewer' does not satisfy the $lane lane (task lane needs code-reviewer)." ;;
  esac
}

if [ "$lane" = doc ]; then
  nonmd="$(printf '%s\n' "$outgoing" | grep -v '\.md$' || true)"
  [ -n "$nonmd" ] && deny "the doc lane (docs/* branch or direct-to-main) is *.md-only; non-.md outgoing: $(printf '%s' "$nonmd" | tr '\n' ' ')— use a task branch + PR."
  require_receipt
else
  require_receipt
  uv run poe check >/dev/null 2>&1 || deny "backend gates are red — run 'uv run poe check', fix, re-review, then push."
  if printf '%s\n' "$outgoing" | grep -q '^web/'; then
    uv run poe web-check >/dev/null 2>&1 || deny "frontend gates are red — run 'uv run poe web-check', fix, re-review, then push."
  fi
fi
exit 0
