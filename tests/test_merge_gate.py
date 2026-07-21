"""Why: require-codex-approval.sh is the LAST line between the loop and an
unapproved merge — the owner's no-exceptions gate. Its bash state machine
(flag tokenizer, freshness compare, fail-closed GraphQL walk) is exactly
where regressions have bitten before (H8/H13 on the watcher), and reasoning-
only review has approved broken shell twice (class 7) — so these EXECUTE the
real hook against a fake `gh` serving canned worlds and pin the allow/deny
exit codes. Deny = exit 2 (the PreToolUse contract; anything else fails
OPEN, which is the one thing this hook must never do).
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
HOOK = (REPO_ROOT / ".claude" / "hooks" / "require-codex-approval.sh").as_posix()

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

HEAD_SHA = "abc1234def5678900000000000000000000000ff"

# canned `gh` — routes on the argument text, reads the world from env vars.
# WORLD_EXPECT_REF makes `pr view` answer ONLY when the hook passed the ref
# the tokenizer was supposed to extract — a mis-tokenized ref (e.g. --squash
# taken as the PR) resolves nothing and the hook must fail CLOSED.
_FAKE_GH = """#!/usr/bin/env bash
args="$*"
case "$args" in
  *"repo view"*) echo 'acme/repo' ;;
  *"pr view"*)
    if [ -n "$WORLD_EXPECT_REF" ]; then
      case "$args" in
        "pr view $WORLD_EXPECT_REF "*) : ;;
        *) echo ''; exit 0 ;;
      esac
    fi
    case "$args" in
      *number*) echo "$WORLD_PR" ;;
      *headRefOid*) echo "$WORLD_HEAD" ;;
    esac
    ;;
  *graphql*) printf '%s\\n' "$WORLD_GRAPHQL_LINE" ;;
  *reactions*'content=="+1"'*) printf '%s\\n' "$WORLD_PLUS1_LINE" ;;
  *reactions*) printf '%s\\n' "$WORLD_REACTIONS" ;;
  *commits/*) printf '%s\\n' "$WORLD_HEAD_LINE" ;;
  *) echo '' ;;
esac
exit 0
"""


def _run_hook(
    tmp_path: Path,
    command: str,
    *,
    expect_ref: str = "",
    reactions: str = "+1",
    plus1_line: str = "2026-07-17T10:06:00Z 2000",
    head_line: str = "2026-07-17T09:00:00Z 1000",
    graphql_line: str = "false\tnull\t0",
    pr: str = "77",
) -> subprocess.CompletedProcess[str]:
    gh = tmp_path / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8", newline="\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path.as_posix()}:{env['PATH']}"
    env["WORLD_EXPECT_REF"] = expect_ref
    env["WORLD_PR"] = pr
    env["WORLD_HEAD"] = HEAD_SHA
    env["WORLD_REACTIONS"] = reactions
    env["WORLD_PLUS1_LINE"] = plus1_line
    env["WORLD_HEAD_LINE"] = head_line
    env["WORLD_GRAPHQL_LINE"] = graphql_line
    payload = '{"tool_input":{"command":"' + command + '"}}'
    return subprocess.run(
        [str(BASH), HOOK],
        input=payload,
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )


def test_non_merge_commands_are_not_engaged(tmp_path: Path) -> None:
    """The gate must be invisible to every other command — engaging (and
    possibly denying) on ordinary pushes would freeze the whole loop."""
    result = _run_hook(tmp_path, "git push origin task/X")
    assert result.returncode == 0


def test_fresh_plus_one_allows_the_merge(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, "gh pr merge 77 --squash")
    assert result.returncode == 0, result.stderr


def test_tokenizer_skips_valueless_flags_before_the_ref(tmp_path: Path) -> None:
    """`gh pr merge --squash 123`: --squash must be skipped, 123 is the PR.
    A tokenizer that grabs '--squash' resolves no PR and fails closed — the
    EXPECT_REF fake answers only when the hook asked about 123."""
    result = _run_hook(tmp_path, "gh pr merge --squash 123", expect_ref="123")
    assert result.returncode == 0, result.stderr


def test_tokenizer_skips_value_taking_flags_and_their_values(tmp_path: Path) -> None:
    """`-R acme/repo 45`: -R consumes 'acme/repo' — a tokenizer that reads
    the repo name as the PR ref resolves nothing."""
    result = _run_hook(tmp_path, "gh pr merge -R acme/repo 45", expect_ref="45")
    assert result.returncode == 0, result.stderr


def test_api_url_merges_are_engaged_and_resolved(tmp_path: Path) -> None:
    """The REST spelling (`pulls/N/merge`) must not bypass the gate, and the
    PR number comes from the URL."""
    result = _run_hook(tmp_path, "gh api -X PUT repos/acme/repo/pulls/77/merge", expect_ref="77")
    assert result.returncode == 0, result.stderr


def test_eyes_blocks_even_with_a_plus_one(tmp_path: Path) -> None:
    """eyes = still reviewing: a +1 from an earlier round must not authorize
    a merge while a NEW review is in flight."""
    result = _run_hook(tmp_path, "gh pr merge 77", reactions="+1\neyes")
    assert result.returncode == 2
    assert "still reviewing" in result.stderr


def test_no_plus_one_blocks(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, "gh pr merge 77", reactions="", plus1_line="")
    assert result.returncode == 2
    assert "NOT approved" in result.stderr


def test_stale_plus_one_blocks(tmp_path: Path) -> None:
    """The +1 must be newer than the head commit — otherwise a follow-up
    commit rides an approval that never saw it (S4's review race,
    mechanized)."""
    result = _run_hook(
        tmp_path,
        "gh pr merge 77",
        plus1_line="2026-07-17T08:00:00Z 500",
        head_line="2026-07-17T09:00:00Z 1000",
    )
    assert result.returncode == 2
    assert "predates" in result.stderr


def test_graphql_failure_fails_closed(tmp_path: Path) -> None:
    """An empty/errored thread query means UNKNOWN thread state — unknown
    must block, not pass (the difference between a gate and a suggestion)."""
    result = _run_hook(tmp_path, "gh pr merge 77", graphql_line="")
    assert result.returncode == 2
    assert "fail-closed" in result.stderr


def test_unresolved_codex_threads_block(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, "gh pr merge 77", graphql_line="false\tnull\t2")
    assert result.returncode == 2
    assert "unresolved" in result.stderr


def test_unresolvable_pr_fails_closed(tmp_path: Path) -> None:
    """If gh can't name the PR it would merge, the hook cannot verify
    anything about it — block."""
    result = _run_hook(tmp_path, "gh pr merge 99", expect_ref="123")  # fake answers only 123
    assert result.returncode == 2
    assert "cannot resolve" in result.stderr
