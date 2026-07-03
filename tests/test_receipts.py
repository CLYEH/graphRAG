"""Why: the review receipt is the CPU gate for LOOP steps 4-5 — a reviewer PASS
is bound to a content tree hash and the push gate recomputes it. Two
reasoning-only review passes approved these scripts while they were broken at
runtime (git rejects mktemp's zero-byte file as an index, so no receipt could
ever be stamped and every push was dead-locked). These tests EXECUTE the
round-trip so a non-functional receipt mechanism can never ship silently again.
"""

from __future__ import annotations

import os
import shutil
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
STAMP_SCRIPT = (REPO_ROOT / ".claude" / "hooks" / "write-review-receipt.sh").as_posix()

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = cwd.as_posix()
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


@pytest.fixture()
def toy_repo(tmp_path: Path) -> Path:
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    # mirror the real repo: receipts are gitignored so stamping never perturbs the hash
    (tmp_path / ".gitignore").write_text(".claude/receipts/\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("hello\n", encoding="utf-8")
    return tmp_path


def _stamp(repo: Path) -> str:
    assert BASH is not None
    result = _run([BASH, STAMP_SCRIPT, "code-reviewer"], repo)
    assert result.returncode == 0, f"stamp failed: {result.stderr or result.stdout}"
    receipt = (repo / ".claude" / "receipts" / "review").read_text(encoding="utf-8").split()
    tree, reviewer = receipt[0], receipt[1]
    assert reviewer == "code-reviewer"
    assert len(tree) == 40, f"not a tree hash: {tree!r}"  # empty/garbage = dead mechanism
    return tree


def test_stamp_is_deterministic_for_unchanged_content(toy_repo: Path) -> None:
    assert _stamp(toy_repo) == _stamp(toy_repo)


def test_stamp_changes_when_content_changes(toy_repo: Path) -> None:
    before = _stamp(toy_repo)
    (toy_repo / "a.md").write_text("edited after review\n", encoding="utf-8")
    assert _stamp(toy_repo) != before  # the push gate must see post-PASS edits


def test_receipt_survives_a_faithful_commit(toy_repo: Path) -> None:
    """Committing exactly the reviewed content must not invalidate the receipt:
    the snapshot (tracked+untracked, receipts ignored) equals HEAD's tree."""
    stamped = _stamp(toy_repo)
    subprocess.run(["git", "add", "-A"], cwd=toy_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "reviewed content"], cwd=toy_repo, check=True, capture_output=True
    )
    head_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=toy_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert stamped == head_tree
