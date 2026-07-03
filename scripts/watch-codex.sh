#!/usr/bin/env bash
# watch-codex.sh <pr-number> [interval-seconds] [max-polls]
#
# The standard way to wait for Codex on a PR (LOOP.md step 7). Watches ALL THREE
# channels Codex uses — issue reactions (+1 / eyes), PR reviews (inline threads;
# how "changes wanted" arrives), and issue comments — because no single channel
# carries every verdict (learned on PR #5: a review-only response is invisible
# to reaction/comment polling).
#
# Exit codes (machine-readable):
#   0  = +1 reaction present -> approved; proceed to merge checks
#   10 = a new Codex review or comment arrived after the watch started -> inspect
#        and triage it per LOOP.md step 7
#   20 = timeout with no fresh Codex response -> poke `@codex review` or escalate
#
# Notes: GitHub offers no push channel to a local machine (webhooks need a public
# endpoint), so polling is the floor. Codex takes ~3-4 min per review round;
# the default 30s interval keeps mean detection latency ~15s at 4 API calls/poll.
set -o pipefail

PR="${1:?usage: watch-codex.sh <pr-number> [interval-seconds] [max-polls]}"
INTERVAL="${2:-30}"
MAX_POLLS="${3:-60}"
BOT='chatgpt-codex-connector[bot]'
BOT_PREFIX='chatgpt-codex-connector' # GraphQL login lacks the [bot] suffix

repo="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')" || exit 20
owner="${repo%%/*}"
name="${repo##*/}"
START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "watching PR #$PR on $repo since $START (interval ${INTERVAL}s, max $MAX_POLLS polls)"

for i in $(seq 1 "$MAX_POLLS"); do
  sleep "$INTERVAL"
  # --paginate everywhere: list endpoints return oldest-first pages of 30, so an
  # unpaginated call on a long PR would miss the fresh response and fake a timeout.
  # jq runs per page under --paginate, so counts are summed and lists re-joined.
  reactions="$(gh api --paginate "repos/$repo/issues/$PR/reactions" \
    --jq "[.[]|select(.user.login==\"$BOT\")|.content]|join(\",\")" 2>/dev/null | paste -sd, - | sed 's/^,*//;s/,*$//')"
  newrev="$(gh api --paginate "repos/$repo/pulls/$PR/reviews" \
    --jq "[.[]|select((.user.login|startswith(\"$BOT_PREFIX\")) and .submitted_at > \"$START\")]|length" 2>/dev/null | awk '{s+=$1} END {print s+0}')"
  newc="$(gh api --paginate "repos/$repo/issues/$PR/comments" \
    --jq "[.[]|select((.user.login|startswith(\"$BOT_PREFIX\")) and .created_at > \"$START\")]|length" 2>/dev/null | awk '{s+=$1} END {print s+0}')"
  unresolved="$(gh api graphql \
    -f query="{repository(owner:\"$owner\",name:\"$name\"){pullRequest(number:$PR){reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{author{login}}}}}}}}" \
    --jq "[.data.repository.pullRequest.reviewThreads.nodes[]|select(.isResolved==false and (.comments.nodes[0].author.login|startswith(\"$BOT_PREFIX\")))]|length" 2>/dev/null)"
  echo "[poll $i] PR#$PR reactions=[$reactions] new_reviews=${newrev:-?} new_comments=${newc:-?} unresolved_threads=${unresolved:-?}"
  if printf '%s' "$reactions" | grep -q '+1'; then
    plus1_at="$(gh api --paginate "repos/$repo/issues/$PR/reactions" \
      --jq "[.[]|select(.user.login==\"$BOT\" and .content==\"+1\")|.created_at]|max // empty" 2>/dev/null | sort | tail -n1)"
    echo "RESULT: +1 — approved (reacted at ${plus1_at:-unknown}; the merge hook still verifies it is newer than the head commit)."
    exit 0
  fi
  if [ "${newrev:-0}" -gt 0 ] || [ "${newc:-0}" -gt 0 ]; then
    echo "RESULT: new Codex response — inspect and triage it (LOOP.md step 7)."
    exit 10
  fi
done
echo "RESULT: timeout — no fresh Codex response. Poke '@codex review' or escalate."
exit 20
