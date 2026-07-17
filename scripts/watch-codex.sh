#!/usr/bin/env bash
# watch-codex.sh <pr-number> [interval-seconds] [max-polls] [--anchor <iso-ts>]
#
# The standard way to wait for Codex on a PR (LOOP.md step 7). Watches ALL THREE
# channels Codex uses — issue reactions (+1 / eyes), PR reviews (inline threads;
# how "changes wanted" arrives), and issue comments — because no single channel
# carries every verdict (learned on PR #5: a review-only response is invisible
# to reaction/comment polling).
#
# --anchor <iso-ts> (H13): the timestamp of the last Codex event you already
# HANDLED. The bootstrap normally treats the last "@codex review" poke as the
# triage-ack — but the normal flow now triages WITHOUT a poke (fix + resolve;
# pushes auto-trigger the re-review), and without an anchor every later watch
# would exit 10 at bootstrap on that same already-handled event forever. Pass
# the handled event's own timestamp (the watcher prints it in its RESULT
# lines) and "unprocessed" becomes "newer than the last HANDLED event".
#
# Exit codes (machine-readable):
#   0  = +1 reaction present -> approved; proceed to merge checks
#   10 = a new Codex review or comment arrived after the watch started -> inspect
#        and triage it per LOOP.md step 7
#   20 = timeout with no fresh Codex response -> poke `@codex review` or escalate
#   30 = Codex's only fresh response is its usage-limits message ("You have
#        reached your Codex usage limits") -> quota exhausted; STOP waiting and
#        re-poke after the reset time instead of polling for nothing
#
# Notes: GitHub offers no push channel to a local machine (webhooks need a public
# endpoint), so polling is the floor. Codex takes ~3-4 min per review round;
# the default 30s interval keeps mean detection latency ~15s at 4 API calls/poll.
set -o pipefail

ANCHOR=""
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --anchor)
      ANCHOR="${2:?--anchor needs an ISO-8601 timestamp}"
      shift 2
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done
set -- "${args[@]}"
PR="${1:?usage: watch-codex.sh <pr-number> [interval-seconds] [max-polls] [--anchor <iso-ts>]}"
INTERVAL="${2:-30}"
MAX_POLLS="${3:-60}"
BOT='chatgpt-codex-connector[bot]'
BOT_PREFIX='chatgpt-codex-connector' # GraphQL login lacks the [bot] suffix

repo="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')" || exit 20
owner="${repo%%/*}"
name="${repo##*/}"
START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "watching PR #$PR on $repo since $START (interval ${INTERVAL}s, max $MAX_POLLS polls)"

# Poke->watch race (H8): Codex often answers a poke within SECONDS, so ANY
# response — a quota-limits reply, a review, a plain feedback comment, even
# the +1 — can land before this script's START and be invisible to every
# created_at > START filter below. Bootstrap: anything the bot did AFTER the
# last human "@codex review" poke is an UNPROCESSED response; classify the
# NEWEST such event exactly like the poll loop would have (+1 -> 0,
# quota-limits comment -> 30, review/plain comment -> 10). With no poke on
# record, NOTHING was ever triage-acknowledged (pokes are the ack), so the
# empty anchor means epoch: every bot event is unprocessed — this covers a
# PR whose FIRST auto-review hits a quota outage before any poke exists.
# gh's EMBEDDED --jq only (no standalone jq on the host); per-page maxes fold
# through sort — ISO-8601 timestamps sort lexically.
boot_poke="$(gh api --paginate "repos/$repo/issues/$PR/comments" \
  --jq "[.[]|select(((.user.login|startswith(\"$BOT_PREFIX\"))|not) and (.body|test(\"@codex review\")))|.created_at]|max // empty" 2>/dev/null | grep -v '^null$' | sort | tail -n1)"
boot_quota="$(gh api --paginate "repos/$repo/issues/$PR/comments" \
  --jq "[.[]|select((.user.login|startswith(\"$BOT_PREFIX\")) and (.body|test(\"reached your Codex usage limits\";\"i\")))|.created_at]|max // empty" 2>/dev/null \
  | grep -v '^null$' | sort | tail -n1)"
boot_botc="$(gh api --paginate "repos/$repo/issues/$PR/comments" \
  --jq "[.[]|select((.user.login|startswith(\"$BOT_PREFIX\")) and ((.body|test(\"reached your Codex usage limits\";\"i\"))|not))|.created_at]|max // empty" 2>/dev/null | grep -v '^null$' | sort | tail -n1)"
boot_review="$(gh api --paginate "repos/$repo/pulls/$PR/reviews" \
  --jq "[.[]|select(.user.login|startswith(\"$BOT_PREFIX\"))|.submitted_at]|max // empty" 2>/dev/null | grep -v '^null$' | sort | tail -n1)"
boot_react="$(gh api --paginate "repos/$repo/issues/$PR/reactions" \
  --jq "[.[]|select(.user.login==\"$BOT\" and .content==\"+1\")|.created_at]|max // empty" 2>/dev/null | grep -v '^null$' | sort | tail -n1)"
# +1 takes PRECEDENCE over same-burst events: Codex's approval arrives with
# its own "no major issues" comment seconds apart, and classifying that
# comment as triage work would misread an approval as findings. The merge
# hook still independently verifies the +1 is newer than the head commit.
#
# Two tie rules, one per anchor kind (H13): against the POKE, ties count as
# AFTER (created_at has one-second resolution; a bot reply sharing the poke's
# exact second can only BE the reply — triage cycles take minutes). Against
# an explicit --anchor, ties count as PROCESSED — the anchor IS the handled
# event's own timestamp, and it must not re-trigger on itself. The LATER of
# the two anchors governs.
processed() {
  # $1 = a bot event's timestamp; true (0) when it was already handled
  local t="$1"
  if [ -n "$ANCHOR" ] && { [ -z "$boot_poke" ] || [[ ! "$ANCHOR" < "$boot_poke" ]]; }; then
    [[ ! "$t" > "$ANCHOR" ]] # t <= anchor → processed
  else
    [[ "$t" < "$boot_poke" ]] # t < poke → processed (ties = the reply)
  fi
}
if [ -n "$boot_react" ] && ! processed "$boot_react"; then
  echo "RESULT: +1 — approved (reacted at $boot_react, before this watch started; the merge hook still verifies it is newer than the head commit)."
  exit 0
fi
newest=""
verdict=""
# verdict:timestamp pairs — %%:* takes the verdict, #*: keeps the full
# timestamp (only the FIRST colon splits)
for pair in "30:$boot_quota" "10:$boot_botc" "10:$boot_review"; do
  t="${pair#*:}"
  [ -n "$t" ] || continue
  processed "$t" && continue
  if [ -z "$newest" ] || [[ "$t" > "$newest" ]]; then
    newest="$t"
    verdict="${pair%%:*}"
  fi
done
case "$verdict" in
  10)
    echo "RESULT: Codex responded at $newest, before this watch started — inspect and triage it (LOOP.md step 7; after triaging without a poke, pass --anchor $newest to the next watch)."
    exit 10
    ;;
  30)
    echo "RESULT: Codex is OUT OF QUOTA (limits message at $newest is its latest response to the last poke) — stop waiting; re-poke '@codex review' after the quota window resets."
    exit 30
    ;;
esac

for i in $(seq 1 "$MAX_POLLS"); do
  sleep "$INTERVAL"
  # --paginate everywhere: list endpoints return oldest-first pages of 30, so an
  # unpaginated call on a long PR would miss the fresh response and fake a timeout.
  # jq runs per page under --paginate, so counts are summed and lists re-joined.
  reactions="$(gh api --paginate "repos/$repo/issues/$PR/reactions" \
    --jq "[.[]|select(.user.login==\"$BOT\")|.content]|join(\",\")" 2>/dev/null | paste -sd, - | sed 's/^,*//;s/,*$//')"
  newrev="$(gh api --paginate "repos/$repo/pulls/$PR/reviews" \
    --jq "[.[]|select((.user.login|startswith(\"$BOT_PREFIX\")) and .submitted_at > \"$START\")]|length" 2>/dev/null | awk '{s+=$1} END {print s+0}')"
  # ONE fetch derives both comment counts (total fresh, quota-message subset):
  # two separate fetches could race a comment landing between them and mint a
  # false exit-30 that the next watch would miss (START advances past it).
  comment_counts="$(gh api --paginate "repos/$repo/issues/$PR/comments" \
    --jq "[.[]|select((.user.login|startswith(\"$BOT_PREFIX\")) and .created_at > \"$START\")] | \"\(length) \([.[]|select(.body|test(\"reached your Codex usage limits\";\"i\"))]|length)\"" 2>/dev/null \
    | awk '{a+=$1; b+=$2} END {print a+0, b+0}')"
  newc="${comment_counts%% *}"
  quota="${comment_counts##* }"
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
    # Quota check BEFORE the triage verdict: the limits message arrives as a
    # plain bot comment, and treating it as "a response to triage" would send
    # the loop into a pointless inspect step while every further poll (and
    # every re-poke) burns time against an exhausted quota. Both counts come
    # from the same fetch above, so this comparison cannot race a comment
    # arriving mid-poll.
    if [ "${newrev:-0}" -eq 0 ] && [ "${newc:-0}" -gt 0 ] && [ "${quota:-0}" -ge "${newc:-0}" ]; then
      echo "RESULT: Codex is OUT OF QUOTA (its only fresh response is the usage-limits message) — stop waiting; re-poke '@codex review' after the quota window resets."
      exit 30
    fi
    # surface the newest fresh event's timestamp: it is the --anchor a later
    # watch needs after this event is triaged WITHOUT a poke (H13)
    latest_rev="$(gh api --paginate "repos/$repo/pulls/$PR/reviews" \
      --jq "[.[]|select((.user.login|startswith(\"$BOT_PREFIX\")) and .submitted_at > \"$START\")|.submitted_at]|max // empty" 2>/dev/null | grep -v '^null$' | sort | tail -n1)"
    latest_com="$(gh api --paginate "repos/$repo/issues/$PR/comments" \
      --jq "[.[]|select((.user.login|startswith(\"$BOT_PREFIX\")) and .created_at > \"$START\")|.created_at]|max // empty" 2>/dev/null | grep -v '^null$' | sort | tail -n1)"
    latest_ts="$latest_rev"
    if [ -n "$latest_com" ] && { [ -z "$latest_ts" ] || [[ "$latest_com" > "$latest_ts" ]]; }; then
      latest_ts="$latest_com"
    fi
    echo "RESULT: new Codex response at ${latest_ts:-unknown} — inspect and triage it (LOOP.md step 7; after triaging without a poke, pass --anchor ${latest_ts:-<its-timestamp>} to the next watch)."
    exit 10
  fi
done
echo "RESULT: timeout — no fresh Codex response. Poke '@codex review' or escalate."
exit 20
