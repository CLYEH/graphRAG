#!/usr/bin/env bash
# PreToolUse gate on `git push` AND `gh pr create`: CPU-verifies the loop's
# feed-forward claims at the transition point instead of trusting the agent's
# narrative. `gh pr create` is gated too because it can ITSELF push an
# unpushed branch (per the gh manual) — a sibling API producing the push
# effect.
#
#   Task-branch push (full lane):
#     - requires a code-reviewer PASS receipt whose tree hash matches the
#       current content (see write-review-receipt.sh) — editing anything after
#       the review makes the push mechanically impossible;
#     - task/FE* additionally requires a browser-QA receipt for the same tree
#       (H10, LOOP.md step 4 FE-only: the Claude in Chrome pass ran on exactly
#       this content — write-browser-qa-receipt.sh refuses to stamp without
#       evidence artifacts);
#     - re-runs `uv run poe check` itself (and `poe web-check` when web/ files
#       are outgoing) — "gates were green" is re-executed, not believed.
#   Direct push to main (doc-only fast lane, LOOP.md):
#     - every outgoing file must be *.md, else denied (use a PR);
#     - requires a doc-reviewer (or code-reviewer) receipt matching the content.
#       CI-green on the SHA is enforced server-side by branch protection.
#
# Like require-codex-approval.sh this is local, honest-agent enforcement.
# Fail-closed. Wired via .claude/settings.json (matcher Bash|PowerShell).
#
# SCOPE (deliberate, PR #64): this hook reads the COMMAND TEXT, so it can only
# recognize the natural spellings of a transfer. It does NOT close the
# command-grammar surface — git aliases, option abbreviations, refspec and tag
# spellings, config routes (push.default / remote.*.push / remote.*.mirror),
# wildcards, all-ref forms — because that surface is unbounded at this layer:
# each spelling closed reveals another. The structural answer is a git
# **pre-push hook** (task H12), which git calls with the ALREADY-RESOLVED
# (local_ref, local_sha, remote_ref, remote_sha) tuples — after aliases,
# config and refspecs are expanded — where receipts can be checked against
# what is actually being sent, in a handful of lines and with no grammar to
# bypass. Until then the independent backstops stand: server-side branch
# protection and the Codex +1 merge gate (require-codex-approval.sh).
# ONE surface stays here permanently even after H12: `gh pr create --head`
# opens a PR from an already-remote ref and skips pushing entirely (gh
# manual), so NO pre-push hook can ever see it — the flag is banned below.
# The same text layer over-blocks in one visible way: the FE token scan below
# reads the WHOLE payload, so an incidental mention of another FE branch (a PR
# title naming task/FE7 while on task/FE0) denies. Fail-closed and cheap to
# work around (pass PR bodies via --body-file, whose text never enters the
# command); H12's resolved tuples remove the ambiguity entirely.
set -o pipefail
deny() { printf 'push-gate: %s\n' "$1" >&2; exit 2; }

payload="$(cat)"
# engage on `git [flags] push` incl. `git -C <path> push` and `git --git-dir=<p> push`
# (must not engage on e.g. `git log push-fix`) — AND on gh PR creation, which
# can push the branch itself (the same effect through a sibling command).
flags='([[:space:]]+-[A-Za-z-]+(=[^[:space:]]+|[[:space:]]+[^[:space:]]+)?)*'
gh_engaged=0
printf '%s' "$payload" | grep -Eq "gh${flags}[[:space:]]+pr${flags}[[:space:]]+(create|new)\b" && gh_engaged=1
printf '%s' "$payload" | grep -Eq "git${flags}[[:space:]]+push\b" || [ "$gh_engaged" = 1 ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || deny "cannot cd to the project dir -> blocked."

# gh --head selects an ALREADY-REMOTE ref and, per the gh manual, SKIPS
# pushing — so no local receipt can vouch for the SHA the PR opens from, and
# this is the one transfer surface H12's pre-push hook can never cover
# (nothing is pushed, so no pre-push hook runs). The flag is banned outright:
# gh defaults to the current branch, which the receipts do cover.
# `-[defw]*H` covers pflag's CLUSTERS: gh pr create's boolean shorthands are
# -d/-e/-f/-w, and pflag lets a boolean cluster carry a value-taking shorthand
# (`-fH x` == `--fill --head x` — reviewer-executed bypass of the unclustered
# ban). The set is gh's booleans TODAY; a new gh boolean shorthand extends it.
# The anchor keeps the capital H inside a repo VALUE from matching (-RCLYEH/…
# starts -R, which [defw]* cannot consume).
[ "$gh_engaged" = 1 ] && printf '%s' "$payload" | grep -Eq "(^|[[:space:]])(--head([=[:space:]]|$)|-[defw]*H)" &&
  deny "gh --head selects an already-remote ref the local receipts cannot vouch for (and it skips pushing, so no pre-push hook sees it) — drop the flag; gh defaults to the current branch."

git fetch -q origin main 2>/dev/null
current="$(git branch --show-current)"
[ -z "$current" ] && deny "detached HEAD — push from a branch."

# H10: the browser receipt binds THIS WORKTREE, so an FE destination must
# carry it — `HEAD:<dst>`, or the current branch itself. Any other source
# sends a ref the browser pass never saw (a divergent local task/FE1 pushed
# from another checkout). Runs BEFORE the no-op fast path below, which a
# clean checkout would otherwise ride straight out.
# (Refspec RESOLUTION — every spelling, alias and config route — belongs to
# the pre-push hook, H12; this is the FE-shaped minimum that keeps H10's own
# receipt meaningful.)
while IFS= read -r tok; do
  [ -n "$tok" ] || continue
  case "$tok" in
  HEAD:* | +HEAD:*) : ;; # the worktree's own commit
  "$current" | "$current":* | +"$current":* | refs/heads/"$current" | refs/heads/"$current":*)
    [[ "$current" == task/FE* ]] || deny "push FE content from its own checkout or via HEAD:<dst> — the browser-QA receipt binds the WORKING TREE, never the local ref '$tok'."
    ;;
  *)
    deny "push FE content from its own checkout or via HEAD:<dst> — the browser-QA receipt binds the WORKING TREE, never the local ref '$tok' (run the browser pass on that checkout and push from there)."
    ;;
  esac
done < <(printf '%s' "$payload" | grep -Eo "[^[:space:]\"']*task/FE[^[:space:]\"']*" || true)

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

require_browser_receipt_if_fe() {
  # the FE receipt keys on the DESTINATION, not only the checked-out branch:
  # `git push origin HEAD:task/FE1` lands on an FE branch from ANY local name
  # (the doc lane's `:main` refspec detection is the same idea). Runs in BOTH
  # lanes: a docs/* branch can push an FE destination too — lane
  # classification must not outrank the destination.
  if [[ "$current" == task/FE* ]] || printf '%s' "$payload" | grep -q 'task/FE'; then
    local fe_tree bq_tree bq_kind bq_rest ev_path ev_count head_tree
    # the push sends HEAD while the receipts bind the WORKTREE: with untested
    # commits under a worktree restored to stamped content, valid receipts
    # would escort unstamped commits out — the two trees must be IDENTICAL.
    head_tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null)"
    [ "$(snapshot_tree)" = "$head_tree" ] || deny "FE push sends HEAD, but the worktree (which the receipts bind) differs from it — commit exactly the passed content (re-run the pass and re-stamp if it changed), then push."
    # H10: the FE browser pass is tree-bound like the review — its own
    # namespace, so neither receipt kind can satisfy the other's gate
    fe_tree="$(snapshot_tree)"
    [ -f ".claude/receipts/browser-qa-$fe_tree" ] || deny "FE task: no browser-QA receipt for this content — run the Claude in Chrome pass (LOOP.md step 4) and stamp it with the evidence via .claude/hooks/write-browser-qa-receipt.sh; anything edited after the pass needs a re-run + re-stamp."
    read -r bq_tree bq_kind bq_rest < ".claude/receipts/browser-qa-$fe_tree"
    { [ "$bq_tree" = "$fe_tree" ] && [ "$bq_kind" = "browser-qa" ]; } || deny "browser-QA receipt for $fe_tree is corrupt — re-stamp via write-browser-qa-receipt.sh."
    # the evidence must STILL exist non-empty at push time: it is untracked/
    # ignored, so it never enters the bound tree — without this liveness
    # re-check, deleting or truncating it after the stamp would go unnoticed
    # and the PR body would have nothing auditable (bind-time check ≠ invariant)
    ev_count=0
    while IFS= read -r ev_path; do
      [ -n "$ev_path" ] || continue
      [ -s "$ev_path" ] || deny "browser-QA evidence missing or empty at push time: $ev_path — re-run the pass and re-stamp (write-browser-qa-receipt.sh)."
      ev_count=$((ev_count + 1))
    done < <(tail -n +2 ".claude/receipts/browser-qa-$fe_tree")
    [ "$ev_count" -ge 1 ] || deny "browser-QA receipt records no evidence paths — re-stamp with the artifacts."
  fi
}

if [ "$lane" = doc ]; then
  nonmd="$(printf '%s\n' "$outgoing" | grep -v '\.md$' || true)"
  [ -n "$nonmd" ] && deny "the doc lane (docs/* branch or direct-to-main) is *.md-only; non-.md outgoing: $(printf '%s' "$nonmd" | tr '\n' ' ')— use a task branch + PR."
  require_receipt
  require_browser_receipt_if_fe
else
  require_receipt
  require_browser_receipt_if_fe
  uv run poe check >/dev/null 2>&1 || deny "backend gates are red — run 'uv run poe check', fix, re-review, then push."
  if printf '%s\n' "$outgoing" | grep -q '^web/'; then
    uv run poe web-check >/dev/null 2>&1 || deny "frontend gates are red — run 'uv run poe web-check', fix, re-review, then push."
  fi
fi
exit 0
