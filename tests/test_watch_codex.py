"""Why: watch-codex.sh is the loop's Codex gate sensor — a wrong exit code
either merges blind (false 0), stalls the loop forever (false 10 — the H13
bug: an event triaged WITHOUT a poke re-triggers every later watch at
bootstrap), or pokes into a quota burn. Shell infra must be EXECUTED to be
reviewed (class 7; the receipts precedent): these tests run the real script
against a fake `gh` serving canned channel states and pin the exit codes,
including both anchor tie rules.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


def _find_bash() -> str | None:
    """Prefer Git for Windows' bash — System32 bash is WSL and chokes on Windows paths."""
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    bash = shutil.which("bash")
    if bash and "system32" not in bash.lower():
        return bash
    return None


BASH = _find_bash()
REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHER = (REPO_ROOT / "scripts" / "watch-codex.sh").as_posix()

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

# One canned world: a human poke, then a bot review AFTER it (the event the
# operator triaged without poking again), no +1, no quota message.
POKE_TS = "2026-07-17T10:00:00Z"
REVIEW_TS = "2026-07-17T10:05:00Z"

_FAKE_GH = """#!/usr/bin/env bash
# canned `gh` — routes by argument content, reads the world from env vars
args="$*"
case "$args" in
  *"repo view"*) echo '{"nameWithOwner":"acme/repo"}' ;;
  *graphql*) echo '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}' ;;
  *reactions*--jq*content*|*reactions*)
    # reactions list: only a +1 when WORLD_PLUS1 is set
    if [ -n "$WORLD_PLUS1" ]; then
      case "$args" in
        *'.content=="+1"'*) echo "$WORLD_PLUS1" ;;
        *) echo '+1' ;;
      esac
    else
      echo ''
    fi
    ;;
  *comments*'@codex review'*) echo "$WORLD_POKE" ;;
  *comments*'usage limits'*'not'*) echo "$WORLD_BOTC" ;;   # non-quota bot comments
  *comments*'usage limits'*) echo '' ;;                     # quota comments: none
  *comments*) echo '0 0' ;;                                 # poll-loop combined counts
  *reviews*submitted_at*'>'*) echo '0' ;;                   # poll-loop fresh reviews
  *reviews*) echo "$WORLD_REVIEW" ;;
  *) echo '' ;;
esac
exit 0
"""


def _run_watcher(tmp_path: Path, *extra: str, plus1: str = "") -> subprocess.CompletedProcess[str]:
    gh = tmp_path / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8", newline="\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path.as_posix()}:{env['PATH']}"
    env["WORLD_POKE"] = POKE_TS
    env["WORLD_REVIEW"] = REVIEW_TS
    env["WORLD_BOTC"] = ""  # the bot response arrived as a REVIEW, not a comment
    env["WORLD_PLUS1"] = plus1
    # interval 0 / max-polls 1 keeps the poll path instant when reached
    cmd = [str(BASH), WATCHER, "77", "0", "1", *extra]
    return subprocess.run(
        cmd, cwd=REPO_ROOT, env=env, capture_output=True, encoding="utf-8", timeout=120
    )


def test_without_anchor_a_handled_review_retriggers_at_bootstrap(tmp_path: Path) -> None:
    """The H13 bug as the ANCHORLESS baseline: the review at 10:05 was already
    triaged (fix + resolve — no poke, pushes auto-trigger re-review), yet a
    fresh watch classifies it as unprocessed: exit 10 at bootstrap, forever."""
    result = _run_watcher(tmp_path)
    assert result.returncode == 10, result.stdout + result.stderr
    assert REVIEW_TS in result.stdout  # the ts the operator needs for --anchor
    assert "--anchor" in result.stdout  # the RESULT line teaches the fix


def test_anchor_marks_the_handled_review_processed(tmp_path: Path) -> None:
    """With --anchor set to the handled event's OWN timestamp, the bootstrap
    stops re-triggering (ties = processed — the anchor IS that event) and the
    watch proceeds to polling: timeout 20 in this quiet world, not 10."""
    result = _run_watcher(tmp_path, "--anchor", REVIEW_TS)
    assert result.returncode == 20, result.stdout + result.stderr


def test_events_newer_than_the_anchor_still_trigger(tmp_path: Path) -> None:
    """The anchor must not eat FUTURE events: anchored before the review's
    timestamp, the review is unprocessed and bootstrap exits 10."""
    result = _run_watcher(tmp_path, "--anchor", "2026-07-17T10:04:59Z")
    assert result.returncode == 10, result.stdout + result.stderr


def test_a_plus_one_newer_than_the_anchor_approves(tmp_path: Path) -> None:
    """Approval outranks the triage verdict at bootstrap, anchored or not —
    a +1 after the anchor is exit 0 (the merge hook independently re-verifies
    it against the head commit)."""
    result = _run_watcher(tmp_path, "--anchor", REVIEW_TS, plus1="2026-07-17T10:06:00Z")
    assert result.returncode == 0, result.stdout + result.stderr


def test_poke_tie_still_counts_as_the_reply(tmp_path: Path) -> None:
    """The POKE anchor's tie rule is unchanged (H8): a bot event sharing the
    poke's exact second can only BE the reply — unprocessed, exit 10. Only
    the explicit --anchor flips ties to processed."""
    result = _run_watcher(tmp_path)
    assert result.returncode == 10  # baseline sanity: poke at 10:00, review 10:05
    # same-second world: poke and review both at 10:00:00
    gh_env_review_eq_poke = POKE_TS
    env_result = _run_watcher_with(tmp_path, review_ts=gh_env_review_eq_poke)
    assert env_result.returncode == 10, env_result.stdout + env_result.stderr


def _run_watcher_with(
    tmp_path: Path, *extra: str, review_ts: str, plus1: str = ""
) -> subprocess.CompletedProcess[str]:
    gh = tmp_path / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8", newline="\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path.as_posix()}:{env['PATH']}"
    env["WORLD_POKE"] = POKE_TS
    env["WORLD_REVIEW"] = review_ts
    env["WORLD_BOTC"] = ""
    env["WORLD_PLUS1"] = plus1
    cmd = [str(BASH), WATCHER, "77", "0", "1", *extra]
    return subprocess.run(
        cmd, cwd=REPO_ROOT, env=env, capture_output=True, encoding="utf-8", timeout=120
    )
