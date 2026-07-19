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
import sys
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
GATES_STAMP_SCRIPT = (REPO_ROOT / "scripts" / "stamp_gates_receipt.py").as_posix()

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


def _add_origin(toy_repo: Path, branch: str) -> None:
    """Commit current content to main on an in-repo bare origin, then switch to `branch`."""
    # the in-repo bare origin must never enter the content snapshot or the diff
    (toy_repo / ".gitignore").write_text(".claude/receipts/\norigin.git/\n", encoding="utf-8")
    for cmd in (
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
        ["git", "init", "-q", "--bare", "origin.git"],
        ["git", "remote", "add", "origin", (toy_repo / "origin.git").as_posix()],
        ["git", "push", "-q", "-u", "origin", "HEAD:main"],
        ["git", "switch", "-qc", branch],
    ):
        subprocess.run(cmd, cwd=toy_repo, check=True, capture_output=True)


def test_push_gate_round_trip_on_the_doc_lane(toy_repo: Path) -> None:
    """EXECUTE the gate, not just the stamp (the H3 lesson): on a docs/*
    branch with .md-only outgoing, the gate must deny while no receipt matches
    the content and let the identical content through once its receipt exists.
    The doc lane needs no gates receipts — CI is its backstop."""
    assert BASH is not None
    _add_origin(toy_repo, "docs/x")
    (toy_repo / "a.md").write_text("doc-lane edit\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "docs"], cwd=toy_repo, check=True, capture_output=True)

    denied = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin docs/x")
    assert denied.returncode == 2, f"gate let an unreviewed push through: {denied.stdout}"
    assert "no review receipt" in denied.stderr

    _stamp(toy_repo)
    allowed = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin docs/x")
    assert allowed.returncode == 0, f"gate blocked reviewed content: {allowed.stderr}"


# ---- gates receipts (H15) -------------------------------------------------
# Why: the push gate used to re-run `poe check` inside the PreToolUse hook.
# PreToolUse timeouts FAIL OPEN (only exit 2 blocks), so the slowest gate was
# the least enforced — and the task lane was untestable (a toy repo can never
# pass the real suite). Receipt verification closes both: these tests EXECUTE
# the task lane end-to-end for the first time.


def _stamp_gates(repo: Path, kind: str) -> str:
    result = _run([sys.executable, GATES_STAMP_SCRIPT, kind], repo)
    assert result.returncode == 0, f"gates stamp failed: {result.stderr or result.stdout}"
    tree = result.stdout.split("tree=")[1].split()[0]
    assert (repo / ".claude" / "receipts" / f"gates-{kind}-{tree}").exists()
    return tree


def test_gates_stamp_matches_the_bash_tree_computation(toy_repo: Path) -> None:
    """The stamper is Python (poe-portable on Windows) while the push gate and
    review stamp are bash — this parity assertion is what keeps the two tree
    implementations from drifting silently (the checker-fork lesson)."""
    assert _stamp_gates(toy_repo, "check") == _stamp(toy_repo)


def test_gates_stamp_rejects_unknown_kind(toy_repo: Path) -> None:
    result = _run([sys.executable, GATES_STAMP_SCRIPT, "lol"], toy_repo)
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_push_gate_task_lane_requires_gates_receipts(toy_repo: Path) -> None:
    """Task lane, non-web outgoing: review receipt alone is not enough — the
    gate must name the missing 'check' receipt; once web/ files are outgoing it
    must additionally demand the 'web-check' receipt."""
    assert BASH is not None
    _add_origin(toy_repo, "task/x")
    (toy_repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=toy_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "code"], cwd=toy_repo, check=True, capture_output=True)

    _stamp(toy_repo)  # review PASS receipt only
    denied = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin task/x")
    assert denied.returncode == 2, f"reviewed-but-unchecked push went through: {denied.stdout}"
    assert "no green 'check' receipt" in denied.stderr

    _stamp_gates(toy_repo, "check")
    allowed = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin task/x")
    assert allowed.returncode == 0, f"gate blocked green content: {allowed.stderr}"

    # web/ outgoing raises the bar: the check receipt no longer suffices
    (toy_repo / "web").mkdir()
    (toy_repo / "web" / "app.ts").write_text("export {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=toy_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "web"], cwd=toy_repo, check=True, capture_output=True)
    _stamp(toy_repo)
    _stamp_gates(toy_repo, "check")
    denied_web = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin task/x")
    assert denied_web.returncode == 2, f"web outgoing without web-check passed: {denied_web.stdout}"
    assert "no green 'web-check' receipt" in denied_web.stderr

    _stamp_gates(toy_repo, "web")
    allowed_web = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin task/x")
    assert allowed_web.returncode == 0, f"gate blocked green web content: {allowed_web.stderr}"


def test_push_gate_task_lane_gates_receipt_is_content_bound(toy_repo: Path) -> None:
    """The receipt must bind to the exact tree, not merely exist: edit after a
    green run, re-review ONLY (fresh review receipt, stale gates receipt) —
    the gate must still deny, naming the check receipt. This is what makes
    'edited after the suite passed' mechanically unpushable."""
    assert BASH is not None
    _add_origin(toy_repo, "task/x")
    (toy_repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=toy_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "code"], cwd=toy_repo, check=True, capture_output=True)
    _stamp(toy_repo)
    _stamp_gates(toy_repo, "check")

    (toy_repo / "code.py").write_text("x = 2  # edited after the green run\n", encoding="utf-8")
    _stamp(toy_repo)  # review re-stamped for the new tree; gates receipt is stale
    denied = _run([BASH, GATE_SCRIPT], toy_repo, stdin="git push -u origin task/x")
    assert denied.returncode == 2, f"stale gates receipt was accepted: {denied.stdout}"
    assert "no green 'check' receipt" in denied.stderr
