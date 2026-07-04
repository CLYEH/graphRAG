#!/usr/bin/env bash
# probe-codex.sh <pr-number> [max-probes] [sleep-seconds]
#
# Quota-outage recovery loop: poke `@codex review`, let the quota-aware
# watcher classify the response (exit 30 = still out of quota), sleep, retry.
# Stops the moment Codex actually engages (exit 0 = +1, 10 = findings) and
# propagates that exit code; exits 20 if every probe found the quota still
# exhausted. Each probe costs one comment pair on the PR thread — the cap
# keeps that bounded.
set -o pipefail

PR="${1:?usage: probe-codex.sh <pr-number> [max-probes] [sleep-seconds]}"
MAX_PROBES="${2:-8}"
SLEEP="${3:-3600}"

for probe in $(seq 1 "$MAX_PROBES"); do
  echo "[probe $probe/$MAX_PROBES] poking @codex review on PR #$PR at $(date -u +%H:%M:%SZ)"
  gh pr comment "$PR" --body "@codex review — quota-reset probe: the head commit is awaiting re-review (all other gates green)." >/dev/null || exit 20
  bash "$(dirname "$0")/watch-codex.sh" "$PR" 30 12
  verdict=$?
  if [ "$verdict" -ne 30 ]; then
    echo "RESULT: probe got a real response (watcher exit $verdict)"
    exit "$verdict"
  fi
  echo "[probe $probe/$MAX_PROBES] still out of quota; sleeping ${SLEEP}s"
  sleep "$SLEEP"
done
echo "RESULT: quota still exhausted after $MAX_PROBES probes — escalate to the owner."
exit 20
