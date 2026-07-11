#!/usr/bin/env bash
# PreToolUse gate on `git push` AND `gh pr create`: CPU-verifies the loop's
# feed-forward claims at the transition point instead of trusting the agent's
# narrative. `gh pr create` is gated too because it can ITSELF push an
# unpushed branch (per the gh manual) — a sibling API producing the push
# effect (class 9: one guard covers every constructor of the effect;
# Codex #64).
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
# THREAT MODEL (explicit boundary): this gate catches ACCIDENTAL bypasses by
# an honest agent — mistyped refspecs, config surprises, quoting slips, and
# the natural command forms. It does NOT claim to contain an adversarial
# shell author: fully-obfuscated verbs (`p=push; $g $p`, `$(printf ...)`)
# are statically unanalyzable in a Turing-expressive shell, and denying ALL
# substitution everywhere would make ordinary tool use impossible. The
# accepted design point: literal-command discipline for anything push-like,
# substitution+push/pr-word engagement as the fail-closed net, and the
# obfuscation residue consciously out of scope (server-side branch
# protection and the Codex +1 gate remain the independent backstops).
set -o pipefail
deny() { printf 'push-gate: %s\n' "$1" >&2; exit 2; }

# parse the COMMAND out of the PreToolUse JSON envelope: the raw envelope's
# own `":"` key separators false-positive quote-anchored patterns (the
# matching-refspec deny blocked this very PR's push), and descriptions
# should never engage gates. Python is already a hard dependency of this
# hook (the poe re-run), so this adds none; a parse failure falls back to
# the raw payload — over-matching, never under (fail-closed).
raw_payload="$(cat)"
# ... and NORMALIZE it with shell tokenization (shlex): quoted fragments
# concatenate before git ever sees them — `other:task/"FE1"` reads as
# task/FE1 to git but defeats a literal grep, and the same evasion works on
# the engagement verb and every flag pattern (Codex #64 R9, executed repro).
# Unbalanced quoting keeps the un-normalized command (the over-match side).
py_bin="$(command -v python3 || command -v python)" ||
  deny "the push gate needs python3 (or python) on PATH to parse the tool payload — fail-closed (Codex #64 R11: the raw-JSON fallback would false-positive on every envelope)."
# TWO projections of the command (Codex #64 R14): line 1 = shlex-NORMALIZED
# (verb/flag/refspec patterns see shell-collapsed tokens); line 2 = the
# RESIDUE with single-quoted spans removed — bash never expands inside
# single quotes, so Markdown backticks in a quoted --body are NOT
# substitution, while $ inside double quotes IS. The substitution and
# wildcard checks run on the residue, everything else on the normalized.
extracted="$(printf '%s' "$raw_payload" | "$py_bin" -c '
import json, re, shlex, sys
try:
    cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
except Exception:
    sys.exit(1)
residue = re.sub(r"\x27[^\x27]*\x27", " ", cmd)
try:
    norm = " ".join(shlex.split(cmd, posix=True))
except ValueError:
    norm = cmd
sys.stdout.write(norm.replace("\n", " ") + "\n" + residue.replace("\n", " ") + "\n")
' 2>/dev/null)" || extracted=""
if [ -n "$extracted" ]; then
  payload="$(printf '%s\n' "$extracted" | sed -n 1p)"
  residue="$(printf '%s\n' "$extracted" | sed -n 2p)"
else
  payload="$raw_payload"
  residue="$raw_payload"
fi
[ -n "$payload" ] || payload="$raw_payload"
[ -n "$residue" ] || residue="$payload"
# engage on `git [flags] push` incl. `git -C <path> push` and `git --git-dir=<p> push`
# (must not engage on e.g. `git log push-fix`) — AND on gh PR creation, which
# can push the branch itself (the same effect through a sibling command).
# The gh pattern tolerates global/persistent flags in BOTH positions
# (`gh -R o/r pr create`, `gh pr --repo o/r create`) and the documented
# `gh pr new` alias (Codex #64 R4) — the same flags idiom as the git pattern.
flags='([[:space:]]+-[A-Za-z-]+(=[^[:space:]]+|[[:space:]]+[^[:space:]]+)?)*'
git_engaged=0
gh_engaged=0
printf '%s' "$payload" | grep -Eq "git${flags}[[:space:]]+push\b" && git_engaged=1
printf '%s' "$payload" | grep -Eq "gh${flags}[[:space:]]+pr${flags}[[:space:]]+(create|new)\b" && gh_engaged=1
if [ "$git_engaged" != 1 ] && [ "$gh_engaged" != 1 ]; then
  # substitution can hide the VERB itself (g=git; $g push — Codex #64 R12,
  # P1): a command we cannot statically clear must not be cleared. When
  # substitution syntax coexists with a push/pr word, engage fail-closed —
  # the literal-command rule below then denies it. Plain commands (no
  # substitution, or no such word) exit here as before.
  if printf '%s' "$residue" | grep -Eq '[$`]' &&
    printf '%s' "$payload" | grep -Eq '(^|[[:space:]])(push|pr)([[:space:];&|]|$)'; then
    # trailing class includes shell operators: `$g push; echo` still runs a
    # push before the semicolon (Codex #64 R13)
    : # engaged fail-closed — fall through to the literal-command deny
  else
    exit 0
  fi
fi
cd "${CLAUDE_PROJECT_DIR:-.}" || deny "cannot cd to the project dir -> blocked."
current="$(git branch --show-current)"
[ -z "$current" ] && deny "detached HEAD — push from a branch."

# shell expansion assembles destinations at runtime (HEAD:task/$suffix reads
# as an FE refspec to git but is invisible to every static pattern — Codex
# #64 R11, P1): an engaged command must be LITERAL. Deny substitution syntax
# outright; shlex keeps it un-expanded, so the pattern sees it.
printf '%s' "$residue" | grep -Eq '[$`]' &&
  deny "shell expansion in a push/PR command hides destinations from the gate — write the command literally (no variables, no command substitution; single-quoted TEXT arguments are fine — quotes suppress expansion)."
# wildcard REFSPECS fan out (quoted or not — git receives them either way,
# Codex #64 R15): a token combining a glob star with a ref separator (/ or
# :) denies on the NORMALIZED command, so quoting cannot hide it while
# Markdown stars in prose (no separator in the token) stay legal.
printf '%s' "$payload" | grep -Eq '(/[^[:space:]]*[*]|[*][^[:space:]]*/|:[^[:space:]]*[*]|[*][^[:space:]]*:)' &&
  deny "wildcard refspecs fan out to unstated branches — name each branch explicitly."

# inline config rewrites push semantics for ONE invocation (`git -c
# push.default=matching push` fans out past the persisted-config checks —
# Codex #64 R13, P1): engaged commands take no inline config at all.
printf '%s' "$payload" | grep -Eq '[[:space:]](-c|--config-env)([[:space:]]|=)' &&
  deny "inline git config (-c/--config-env) on a push/PR command rewrites push semantics for that invocation — the gate checks persisted config only; drop the flag."

# all-branch/mirror forms push refs the worktree-bound receipts never spoke
# for (an unreceipted local task/FE* rides along invisibly — Codex #64 R7,
# executed repro) — reject the forms outright; push refs explicitly
printf '%s' "$payload" | grep -Eq -- '--(all|branches|mirror)\b' &&
  deny "all-branch push forms (--all/--branches/--mirror) bypass the content-bound receipts — push the branch explicitly."
# the matching-refspec form (`:` / `+:`) and a `matching` push.default fan out
# to every branch existing on both ends — the same invisible ride for an
# unreceipted local task/FE* (Codex #64 R8): deny the refspec form, and deny
# pushing at all under a matching default (a bare push would fan out too).
printf '%s' "$payload" | grep -Eq "[[:space:]\"']\+?:([[:space:]\"']|\$)" &&
  deny "the matching-refspec push form (: / +:) updates every matching branch — push the branch explicitly."
[ "$(git config --get push.default 2>/dev/null)" = "matching" ] &&
  deny "push.default=matching makes pushes fan out to every matching branch — set push.default to simple, then push explicitly."
# configured push refspecs are the same invisibility one level deeper: with
# remote.<name>.push set, a bare `git push origin` lands wherever the CONFIG
# says (e.g. HEAD:refs/heads/task/FE1) with nothing in the payload at all
# (Codex #64 R10, executed repro) — deny while any are set.
[ -n "$(git config --get-regexp '^remote\..*\.push$' 2>/dev/null)" ] &&
  deny "remote.<name>.push refspecs route bare pushes to unstated destinations — unset them (git config --unset-all remote.<name>.push) and push explicitly."
# remote.<name>.mirror makes MIRROR mode the default for that remote — every
# push becomes an all-ref push with no flag in the payload (Codex #64 R15,
# executed repro; completes the documented config set alongside push.default
# and remote.<name>.push)
while IFS= read -r mirror_key; do
  [ -n "$mirror_key" ] || continue
  # --type=bool canonicalizes yes/on/1 to true (all git-true spellings) and
  # keys, not values, drive the loop — a remote NAME containing "true"
  # cannot false-match (reviewer-verified fix)
  [ "$(git config --type=bool --get "$mirror_key" 2>/dev/null)" = "true" ] &&
    deny "remote.<name>.mirror makes every push a mirror push — unset it and push the branch explicitly."
done < <(git config --get-regexp '^remote\..*\.mirror$' 2>/dev/null | cut -d' ' -f1)
# push.default=upstream sends HEAD to branch.<name>.merge — a CROSS-NAMED
# upstream routes a bare push onto a branch the payload never names (the
# local reviewer executed the work -> FE case; same config family)
case "$(git config --get push.default 2>/dev/null)" in
upstream | tracking) # tracking = documented deprecated synonym (Codex #64 R11)
  upstream_ref="$(git config --get "branch.${current}.merge" 2>/dev/null)"
  if [ -n "$upstream_ref" ] && [ "$upstream_ref" != "refs/heads/$current" ]; then
    deny "an upstream/tracking push.default with a cross-named upstream ($upstream_ref) routes bare pushes onto an unstated branch — push explicitly or align the upstream."
  fi
  ;;
*) : ;;
esac

git fetch -q origin main 2>/dev/null

validate_fe_ref_tokens() {
  # every explicit token naming an FE branch must carry content the
  # receipts bind: HEAD:<dst>, or the CURRENT FE branch itself (bare or as
  # the refspec src). Any other src pushes a ref the worktree receipts
  # never spoke for — `origin task/FE1` from another checkout (Codex #64
  # R6) and `origin other:task/FE1` even ON the FE checkout (R7, both
  # executed repros). Runs BEFORE the no-op fast path: a clean checkout can
  # still push a divergent local FE ref by naming it (R13, executed repro).
  local tok
  while IFS= read -r tok; do
    [ -n "$tok" ] || continue
    case "$tok" in
      HEAD:* | +HEAD:*) : ;; # the worktree's own commit
      "$current" | "$current":* | +"$current":* | refs/heads/"$current" | refs/heads/"$current":*)
        # the checked-out FE branch itself — only valid when we ARE on it
        [[ "$current" == task/FE* ]] || deny "push FE content from its own checkout or via HEAD:<dst> — the receipts bind the WORKING TREE, never the local ref '$tok'."
        ;;
      *)
        # gh's documented head form `--head owner:branch` is NOT a git
        # refspec (Codex #64 R15, over-block): on a gh-only engagement,
        # an owner-qualified token naming the CURRENT FE checkout is the
        # legitimate fully-receipted flow — any other branch still denies
        # (the receipts cannot speak for a branch we are not on).
        if [ "$gh_engaged" = 1 ] && [ "$git_engaged" != 1 ] &&
          [[ "$current" == task/FE* ]] &&
          printf '%s' "$tok" | grep -Eq "^[A-Za-z0-9][A-Za-z0-9-]*:(refs/heads/)?${current}\$"; then
          :
        else
          deny "push FE content from its own checkout or via HEAD:<dst> — the receipts bind the WORKING TREE, never the local ref '$tok' (re-run the pass on that content and push from there)."
        fi
        ;;
    esac
  done < <(printf '%s' "$payload" | grep -Eo "[^[:space:]\"']*task/FE[^[:space:]\"']*" || true)
}
validate_fe_ref_tokens

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
  # (Codex #64 R3 — the doc lane's `:main` refspec detection is the same
  # idea). Any push/PR payload naming task/FE engages, deliberately
  # over-matching (multi-branch and deletion forms included) — fail-closed,
  # like the rest of this hook. Runs in BOTH lanes: the doc lane can push an
  # FE destination too, e.g. `HEAD:task/FE1` from a docs/* branch (Codex #64
  # R5 — lane classification must not outrank the destination).
  if [[ "$current" == task/FE* ]] || printf '%s' "$payload" | grep -q 'task/FE'; then
    local fe_tree bq_tree bq_kind bq_rest ev_path ev_count tok head_tree
    # the push sends HEAD while receipts bind the WORKTREE: with untested
    # commits under a worktree restored to stamped content, valid receipts
    # would escort unstamped commits out (Codex #64 R12, executed repro) —
    # the two trees must be IDENTICAL on an FE push.
    head_tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null)"
    [ "$(snapshot_tree)" = "$head_tree" ] || deny "FE push sends HEAD, but the worktree (which the receipts bind) differs from it — commit exactly the passed content (re-run the pass and re-stamp if it changed), then push."
    # (the FE ref-token legality check runs earlier, BEFORE the no-op fast
    # path — see validate_fe_ref_tokens)
    # H10: the FE browser pass is tree-bound like the review — its own
    # namespace, so neither receipt kind can satisfy the other's gate
    fe_tree="$(snapshot_tree)"
    [ -f ".claude/receipts/browser-qa-$fe_tree" ] || deny "FE task: no browser-QA receipt for this content — run the Claude in Chrome pass (LOOP.md step 4) and stamp it with the evidence via .claude/hooks/write-browser-qa-receipt.sh; anything edited after the pass needs a re-run + re-stamp."
    read -r bq_tree bq_kind bq_rest < ".claude/receipts/browser-qa-$fe_tree"
    { [ "$bq_tree" = "$fe_tree" ] && [ "$bq_kind" = "browser-qa" ]; } || deny "browser-QA receipt for $fe_tree is corrupt — re-stamp via write-browser-qa-receipt.sh."
    # the evidence must STILL exist non-empty at push time: it is untracked/
    # ignored, so it never enters the bound tree — without this liveness
    # re-check, deleting or truncating it after the stamp would go unnoticed
    # and the PR body would have nothing auditable (Codex #64 R2, class 10)
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
