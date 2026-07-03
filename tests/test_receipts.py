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
GATE_SCRIPT = (REPO_ROOT / ".claude" / "hooks" / "require-push-gates.sh").as_posix()

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run(cmd: list[str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = cwd.as_posix()
    # the hooks print UTF-8 (em-dashes in deny messages); Windows' locale codec
    # (cp950 here) would throw mid-read — the alembic/cp950 lesson again
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, encoding="utf-8", input=stdin)


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
    tree = result.stdout.split("tree=")[1].split()[0]
    assert len(tree) == 40, f"not a tree hash: {tree!r}"  # empty/garbage = dead mechanism
    # content-addressed (H5): the receipt lives at receipts/<tree> and names it
    receipt = (repo / ".claude" / "receipts" / tree).read_text(encoding="utf-8").split()
    assert receipt[0] == tree
    assert receipt[1] == "code-reviewer"
    return tree


def test_stamp_is_deterministic_for_unchanged_content(toy_repo: Path) -> None:
    assert _stamp(toy_repo) == _stamp(toy_repo)


def test_stamp_changes_when_content_changes(toy_repo: Path) -> None:
    before = _stamp(toy_repo)
    (toy_repo / "a.md").write_text("edited after review\n", encoding="utf-8")
    assert _stamp(toy_repo) != before  # the push gate must see post-PASS edits


def test_parallel_stamps_coexist(toy_repo: Path) -> None:
    """The H5 point: two reviewed states (parallel task/docs branches) must
    hold receipts SIMULTANEOUSLY — the old single-slot file made the second
    stamp evict the first, forcing a re-review just to switch branches back."""
    first = _stamp(toy_repo)
    (toy_repo / "a.md").write_text("the other branch's content\n", encoding="utf-8")
    second = _stamp(toy_repo)
    assert first != second
    receipts = toy_repo / ".claude" / "receipts"
    assert (receipts / first).exists() and (receipts / second).exists()
    # switching back to the first state re-validates against its own receipt
    (toy_repo / "a.md").write_text("hello\n", encoding="utf-8")
    assert _stamp(toy_repo) == first


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


def test_push_gate_round_trip_on_the_doc_lane(toy_repo: Path) -> None:
    """EXECUTE the gate, not just the stamp (the H3 lesson): on a docs/*
    branch with .md-only outgoing, the gate must deny while no receipt matches
    the content and let the identical content through once its receipt exists.
    Uses the doc lane because the task lane's `poe check` re-run cannot succeed
    inside a toy repo."""
    assert BASH is not None
    # the in-repo bare origin must never enter the content snapshot or the diff
    (toy_repo / ".gitignore").write_text(".claude/receipts/\norigin.git/\n", encoding="utf-8")
    for cmd in (
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
        ["git", "init", "-q", "--bare", "origin.git"],
        ["git", "remote", "add", "origin", (toy_repo / "origin.git").as_posix()],
        ["git", "push", "-q", "-u", "origin", "HEAD:main"],
        ["git", "switch", "-qc", "docs/x"],
    ):
        subprocess.run(cmd, cwd=toy_repo, check=True, capture_output=True)
    (toy_repo / "a.md").write_text("doc-lane edit\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "docs"], cwd=toy_repo, check=True, capture_output=True)

    denied = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin docs/x")
    assert denied.returncode == 2, f"gate let an unreviewed push through: {denied.stdout}"
    assert "no review receipt" in denied.stderr

    _stamp(toy_repo)
    allowed = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin docs/x")
    assert allowed.returncode == 0, f"gate blocked reviewed content: {allowed.stderr}"
