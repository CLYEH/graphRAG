#!/usr/bin/env bash
# PreToolUse gate on `git push`: CPU-verifies the loop's feed-forward claims at
# the transition point instead of trusting the agent's narrative.
#
#   Task-branch push (full lane):
#     - requires a code-reviewer PASS receipt whose tree hash matches the
#       current content (see write-review-receipt.sh) — editing anything after
#       the review makes the push mechanically impossible;
#     - requires green-gates receipts for the same tree: a green `uv run poe
#       check` stamps .claude/receipts/gates-check-<tree> as its final sequence
#       step (scripts/stamp_gates_receipt.py; `poe web-check` likewise stamps
#       gates-web-<tree>, required when web/ files are outgoing). Verifying the
#       stamp replaces the old inline suite re-run: that took minutes, and a
#       PreToolUse hook that outlives its timeout fails OPEN (only exit 2
#       blocks), so the slowest gate was the least enforced (H15).
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

# one snapshot serves every receipt check below — the hook is a single moment
now="$(snapshot_tree)"

require_receipt() {
  # receipts are content-addressed (H5): .claude/receipts/<tree>, one per
  # reviewed state — parallel branches don't clobber each other's stamps
  local tree reviewer rest
  [ -f ".claude/receipts/$now" ] || deny "no review receipt for this content — a reviewer subagent must PASS this exact state (it stamps .claude/receipts/<tree>); anything edited after its PASS needs a re-review."
  read -r tree reviewer rest < ".claude/receipts/$now"
  [ "$tree" = "$now" ] || deny "receipt .claude/receipts/$now is corrupt (names tree '$tree') — re-run the reviewer."
  case "$lane:$reviewer" in
    task:code-reviewer | doc:doc-reviewer | doc:code-reviewer) : ;;
    *) deny "receipt from '$reviewer' does not satisfy the $lane lane (task lane needs code-reviewer)." ;;
  esac
}

require_gates_receipt() {
  # stamped by scripts/stamp_gates_receipt.py as the FINAL step of a green
  # `poe check` / `poe web-check` sequence, so its existence for THIS tree
  # means the whole suite passed on exactly this content (H15)
  local kind="$1" task="$2"
  [ -f ".claude/receipts/gates-$kind-$now" ] || deny "no green '$task' receipt for this exact content — run 'uv run poe $task' (a green run stamps .claude/receipts/gates-$kind-<tree>); anything edited after the green run needs a re-run."
}

if [ "$lane" = doc ]; then
  nonmd="$(printf '%s\n' "$outgoing" | grep -v '\.md$' || true)"
  [ -n "$nonmd" ] && deny "the doc lane (docs/* branch or direct-to-main) is *.md-only; non-.md outgoing: $(printf '%s' "$nonmd" | tr '\n' ' ')— use a task branch + PR."
  require_receipt
else
  require_receipt
  require_gates_receipt check check
  if printf '%s\n' "$outgoing" | grep -q '^web/'; then
    require_gates_receipt web web-check
  fi
fi
exit 0
