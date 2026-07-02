#!/usr/bin/env bash
# PreToolUse guard: block a PR merge unless Codex reacted +1 on the CURRENT head commit.
#
# Rule (no exceptions): `gh pr merge` (or a pulls/<n>/merge API call) is allowed only when
# chatgpt-codex-connector[bot] has reacted +1 AND that +1 is newer than the PR head commit
# (so an unreviewed follow-up commit can't ride a stale approval). Still-reviewing (eyes),
# no/stale +1, or any unresolved Codex review thread => BLOCK.
#
# Reads the PreToolUse JSON payload on stdin. Depends only on `gh` (its bundled --jq,
# including fromdateiso8601) — no jq/python/pwsh and no `date` binary, so it is portable
# across Windows (git-bash), Linux, and macOS (whose BSD `date` lacks GNU `-d`). Fails
# CLOSED: on a detected merge, any inability to verify approval blocks the merge.
# Wired via .claude/settings.json (matcher Bash|PowerShell).
set -o pipefail

BOT='chatgpt-codex-connector[bot]'   # REST login (reactions)
BOT_PREFIX='chatgpt-codex-connector' # GraphQL login has no [bot] suffix; match either via startswith

# Deny = exit 2 with reason on stderr (PreToolUse contract: 2 blocks, stderr goes to Claude).
deny() { printf 'Codex-gate: %s\n' "$1" >&2; exit 2; }

payload="$(cat)"

# Engage only on a merge command. Grep the raw payload so no JSON parser is needed here.
if ! printf '%s' "$payload" | grep -Eq 'gh[[:space:]]+pr[[:space:]]+merge|pulls/[0-9]+/merge'; then
  exit 0
fi

# --- figure out which PR gh will actually merge (number | url | branch | current branch) ---
arg="$(printf '%s' "$payload" \
  | grep -oE 'gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+[^"\\ ]+' \
  | head -n1 | sed -E 's/^gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+//')"
# A leading '-' means the token is a flag (e.g. --squash), not a PR ref.
case "$arg" in -*) arg="" ;; esac
if [ -z "$arg" ]; then
  apinum="$(printf '%s' "$payload" | grep -oE 'pulls/[0-9]+/merge' | grep -oE '[0-9]+' | head -n1)"
  [ -n "$apinum" ] && arg="$apinum"
fi

# Resolve the real PR + head SHA via gh (handles number/url/branch, and current branch when empty).
if [ -n "$arg" ]; then set -- "$arg"; else set --; fi
pr="$(gh pr view "$@" --json number --jq '.number' 2>/dev/null)"
head="$(gh pr view "$@" --json headRefOid --jq '.headRefOid' 2>/dev/null)"
[ -z "$pr" ] && deny "cannot resolve the PR gh would merge (ref='${arg:-<current branch>}') -> blocked."
[ -z "$head" ] && deny "cannot resolve PR #$pr head SHA -> blocked."

repo="$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null)"
[ -z "$repo" ] && deny "cannot resolve the repo -> blocked."
owner="${repo%%/*}"
name="${repo##*/}"

# --- Codex reactions (paginated). Convert timestamps with jq's fromdateiso8601 so no `date`
#     binary is needed (GNU vs BSD differ) — portable across Windows/Linux/macOS. ---
reactions="$(gh api --paginate "repos/$repo/issues/$pr/reactions" \
  --jq ".[]|select(.user.login==\"$BOT\")|.content" 2>/dev/null)"
plus1_line="$(gh api --paginate "repos/$repo/issues/$pr/reactions" \
  --jq ".[]|select(.user.login==\"$BOT\" and .content==\"+1\")|\"\(.created_at) \(.created_at|fromdateiso8601)\"" 2>/dev/null | head -n1)"

if printf '%s\n' "$reactions" | grep -qx 'eyes'; then
  deny "Codex is still reviewing PR #$pr (reaction: eyes). Wait for +1 before merging. No exceptions."
fi
if [ -z "$plus1_line" ]; then
  deny "Codex has NOT approved PR #$pr (no +1 from $BOT; reactions='$(printf '%s' "$reactions" | tr '\n' ',')'). Poke '@codex review', wait for +1, then merge."
fi
read -r plus1_at approved_epoch <<<"$plus1_line"

# --- the +1 must be NEWER than the head commit (reject stale approval of unreviewed commits) ---
head_line="$(gh api "repos/$repo/commits/$head" --jq '.commit.committer.date as $d|"\($d) \($d|fromdateiso8601)"' 2>/dev/null)"
[ -z "$head_line" ] && deny "cannot read head commit date -> blocked."
read -r head_date head_epoch <<<"$head_line"
case "$approved_epoch" in ''|*[!0-9]*) deny "cannot parse +1 timestamp -> blocked." ;; esac
case "$head_epoch" in ''|*[!0-9]*) deny "cannot parse head commit timestamp -> blocked." ;; esac
if [ "$approved_epoch" -le "$head_epoch" ]; then
  deny "Codex +1 ($plus1_at) predates head commit ${head:0:9} ($head_date) -> the head is unreviewed. Re-request '@codex review' and wait for a fresh +1."
fi

# --- no unresolved Codex review threads (paginate ALL pages; fail closed on API error) ---
Q='query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{isResolved comments(first:1){nodes{author{login}}}}}}}}'
JQ='{next:.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage,cur:.data.repository.pullRequest.reviewThreads.pageInfo.endCursor,n:([.data.repository.pullRequest.reviewThreads.nodes[]|select(.isResolved==false and (.comments.nodes[0].author.login|startswith("'"$BOT_PREFIX"'")))]|length)}|"\(.next)\t\(.cur)\t\(.n)"'
cursor=""
unresolved=0
while :; do
  args=( api graphql -f "query=$Q" -F "owner=$owner" -F "name=$name" -F "number=$pr" )
  if [ -z "$cursor" ]; then args+=( -F after=null ); else args+=( -f "after=$cursor" ); fi
  args+=( --jq "$JQ" )
  line="$(gh "${args[@]}" 2>/dev/null)"
  [ -z "$line" ] && deny "cannot verify Codex review threads (GraphQL failed/empty) -> blocked (fail-closed)."
  IFS=$'\t' read -r hasNext cursor n <<<"$line"
  case "$n" in ''|*[!0-9]*) deny "unexpected thread-count '$n' -> blocked (fail-closed)." ;; esac
  unresolved=$((unresolved + n))
  [ "$hasNext" = "true" ] || break
done
if [ "$unresolved" -gt 0 ]; then
  deny "PR #$pr has $unresolved unresolved Codex review thread(s). Address + resolve them, keep a fresh Codex +1, then merge."
fi

# Fresh Codex +1 on the head, not reviewing, no unresolved threads -> allow the merge.
exit 0
