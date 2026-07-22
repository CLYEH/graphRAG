"""Stamp a gates receipt: .claude/receipts/gates-<kind>-<tree>.

Why: the push gate used to re-run `poe check` inside the PreToolUse hook —
minutes of latency per push, and worse: PreToolUse hook timeouts FAIL OPEN
(only exit 2 blocks the tool call), so the slowest gate was also the least
enforced (harness review A1 / TASKS.md H15). Instead, a green `poe check` /
`poe web-check` run stamps a content-addressed receipt as its FINAL sequence
step — the stamp is only reached when every gate before it passed — and the
push gate verifies the stamp in milliseconds. Same honest-agent trust level as
the review receipt; CI remains the server-side backstop.

The tree hash MUST equal what .claude/hooks/write-review-receipt.sh and
require-push-gates.sh compute (tracked + untracked non-ignored files via a
throwaway index; receipts themselves are gitignored so stamping never perturbs
the hash). tests/test_receipts.py asserts this Python/bash parity so the two
implementations cannot drift silently.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

KINDS = ("check", "web")


def _git(args: list[str], env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], env=env, check=True, capture_output=True, encoding="utf-8"
    )
    return result.stdout.strip()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in KINDS:
        print(f"usage: stamp_gates_receipt.py <{'|'.join(KINDS)}>", file=sys.stderr)
        return 2
    kind = sys.argv[1]
    root = Path(_git(["rev-parse", "--show-toplevel"]))

    # git refuses a zero-byte index file: reserve a fresh path, delete it, and
    # let git create the index there (same dance as write-review-receipt.sh)
    fd, tmp = tempfile.mkstemp()
    os.close(fd)
    os.unlink(tmp)
    env = {**os.environ, "GIT_INDEX_FILE": tmp}
    try:
        _git(["-C", str(root), "add", "-A"], env=env)
        tree = _git(["-C", str(root), "write-tree"], env=env)
    finally:
        Path(tmp).unlink(missing_ok=True)
    if len(tree) != 40:
        print(f"snapshot tree computation failed: {tree!r}", file=sys.stderr)
        return 1

    receipts = root / ".claude" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    _gc_old_receipts(receipts)
    stamped_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (receipts / f"gates-{kind}-{tree}").write_text(
        f"{tree} gates-{kind} {stamped_at}\n", encoding="utf-8"
    )
    print(f"gates receipt stamped: kind={kind} tree={tree}")
    return 0


#: Receipts are content-addressed and single-use — once the tree moves on, an
#: old stamp can never validate again, so anything older than this is dead
#: weight (H19: the dir otherwise grows one file per gates run, forever).
GC_MAX_AGE_DAYS = 30


def _gc_old_receipts(receipts: Path) -> None:
    """Best-effort sweep of expired receipts at stamp time. Never fails the
    stamp: a GC error must not turn a green gates run red (the receipt being
    written is the product; the sweep is hygiene)."""
    cutoff = datetime.now(UTC).timestamp() - GC_MAX_AGE_DAYS * 86400
    try:
        entries = list(receipts.iterdir())
    except OSError:
        return  # the "never fails the stamp" claim covers the dir walk too
    for f in entries:
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


if __name__ == "__main__":
    sys.exit(main())
