"""Why: the review receipt is the CPU gate for LOOP steps 4-5 — a reviewer PASS
is bound to a content tree hash and the push gate recomputes it. Two
reasoning-only review passes approved these scripts while they were broken at
runtime (git rejects mktemp's zero-byte file as an index, so no receipt could
ever be stamped and every push was dead-locked). These tests EXECUTE the
round-trip so a non-functional receipt mechanism can never ship silently again.
The H10 browser-QA receipt (FE tasks) rides the same discipline: stamp refuses
without evidence, the gate demands it on task/FE* only, and it is tree-bound.
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
BROWSER_STAMP_SCRIPT = (REPO_ROOT / ".claude" / "hooks" / "write-browser-qa-receipt.sh").as_posix()
GATE_SCRIPT = (REPO_ROOT / ".claude" / "hooks" / "require-push-gates.sh").as_posix()

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

#: the gate engages on the pushing git verb and on the PR-creating gh verb;
#: tests build the stdin payloads from parts so the PreToolUse hook watching
#: THIS repo's tool calls does not engage on the test source itself (it greps
#: command payloads for the verbs)
_PUSH = "git " + "pu" + "sh"
_PR_CREATE = "gh pr " + "cre" + "ate"


def _run(
    cmd: list[str],
    cwd: Path,
    stdin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = cwd.as_posix()
    if extra_env:
        env.update(extra_env)
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


def _origin_and_branch(repo: Path, branch: str) -> None:
    for cmd in (
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
        ["git", "init", "-q", "--bare", "origin.git"],
        ["git", "remote", "add", "origin", (repo / "origin.git").as_posix()],
        ["git", _PUSH.split()[1], "-q", "-u", "origin", "HEAD:main"],
        ["git", "switch", "-qc", branch],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


def test_push_gate_round_trip_on_the_doc_lane(toy_repo: Path) -> None:
    """EXECUTE the gate, not just the stamp (the H3 lesson): on a docs/*
    branch with .md-only outgoing, the gate must deny while no receipt matches
    the content and let the identical content through once its receipt exists.
    Uses the doc lane because the task lane's `poe check` re-run cannot succeed
    inside a toy repo."""
    assert BASH is not None
    # the in-repo bare origin must never enter the content snapshot or the diff
    (toy_repo / ".gitignore").write_text(".claude/receipts/\norigin.git/\n", encoding="utf-8")
    _origin_and_branch(toy_repo, "docs/x")
    (toy_repo / "a.md").write_text("doc-lane edit\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "docs"], cwd=toy_repo, check=True, capture_output=True)

    denied = _run([BASH, GATE_SCRIPT], toy_repo, stdin=f"{_PUSH} -u origin docs/x")
    assert denied.returncode == 2, f"gate let an unreviewed push through: {denied.stdout}"
    assert "no review receipt" in denied.stderr

    _stamp(toy_repo)
    allowed = _run([BASH, GATE_SCRIPT], toy_repo, stdin=f"{_PUSH} -u origin docs/x")
    assert allowed.returncode == 0, f"gate blocked reviewed content: {allowed.stderr}"


def _browser_stamp(repo: Path, *evidence: Path) -> str:
    assert BASH is not None
    result = _run([BASH, BROWSER_STAMP_SCRIPT, *[e.as_posix() for e in evidence]], repo)
    assert result.returncode == 0, f"browser stamp failed: {result.stderr or result.stdout}"
    tree = result.stdout.split("tree=")[1].split()[0]
    assert len(tree) == 40
    receipt = (repo / ".claude" / "receipts" / f"browser-qa-{tree}").read_text(encoding="utf-8")
    fields = receipt.split()
    assert fields[0] == tree and fields[1] == "browser-qa"
    return tree


def test_browser_stamp_refuses_a_claim_without_evidence(toy_repo: Path) -> None:
    """H10: a stamp is a RECORD of a pass, not a claim — no evidence file, no
    receipt (missing args, missing file, and empty file all refuse)."""
    assert BASH is not None
    no_args = _run([BASH, BROWSER_STAMP_SCRIPT], toy_repo)
    assert no_args.returncode != 0
    missing = _run([BASH, BROWSER_STAMP_SCRIPT, "no-such-shot.png"], toy_repo)
    assert missing.returncode != 0 and "missing or empty" in missing.stderr
    empty = toy_repo / "empty.png"
    empty.write_bytes(b"")
    hollow = _run([BASH, BROWSER_STAMP_SCRIPT, empty.as_posix()], toy_repo)
    assert hollow.returncode != 0 and "missing or empty" in hollow.stderr
    receipts = toy_repo / ".claude" / "receipts"
    assert not receipts.exists() or not list(receipts.glob("browser-qa-*"))


def _uv_shim(repo: Path) -> dict[str, str]:
    """A PATH shim so the gate's `uv run poe check` re-run succeeds inside the
    toy repo (the real gates are irrelevant to what these tests pin)."""
    shim = repo / "shim"
    shim.mkdir(exist_ok=True)
    uv = shim / "uv"
    uv.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
    uv.chmod(0o755)
    return {"PATH": shim.as_posix() + os.pathsep + os.environ.get("PATH", "")}


def test_fe_push_gate_requires_the_browser_receipt(toy_repo: Path) -> None:
    """EXECUTE the H10 gate: on a task/FE* branch a code-review receipt alone
    must NOT push (the deny names the browser pass, proving WHICH check
    fired); both receipts pass; the browser receipt is TREE-BOUND (an edit
    after the pass re-blocks even with a fresh review stamp); and a non-FE
    task branch never demands it."""
    assert BASH is not None
    (toy_repo / ".gitignore").write_text(
        ".claude/receipts/\norigin.git/\nshim/\nshot.png\n", encoding="utf-8"
    )
    _origin_and_branch(toy_repo, "task/FE1")
    (toy_repo / "a.md").write_text("fe edit\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "fe"], cwd=toy_repo, check=True, capture_output=True)
    env = _uv_shim(toy_repo)
    shot = toy_repo / "shot.png"
    shot.write_bytes(b"\x89PNG fake-but-non-empty")

    _stamp(toy_repo)  # the code-review receipt alone
    denied = _run([BASH, GATE_SCRIPT], toy_repo, stdin=f"{_PUSH} -u origin task/FE1", extra_env=env)
    assert denied.returncode == 2, f"FE push passed without the browser receipt: {denied.stdout}"
    assert "browser-QA receipt" in denied.stderr  # THIS check fired, not the review one

    _browser_stamp(toy_repo, shot)
    allowed = _run(
        [BASH, GATE_SCRIPT], toy_repo, stdin=f"{_PUSH} -u origin task/FE1", extra_env=env
    )
    assert allowed.returncode == 0, f"gate blocked a fully-receipted FE push: {allowed.stderr}"

    # tree-bound: an edit AFTER the browser pass invalidates its receipt even
    # with a fresh code-review stamp for the new tree
    (toy_repo / "a.md").write_text("edited after the browser pass\n", encoding="utf-8")
    _stamp(toy_repo)
    stale = _run([BASH, GATE_SCRIPT], toy_repo, stdin=f"{_PUSH} -u origin task/FE1", extra_env=env)
    assert stale.returncode == 2 and "browser-QA receipt" in stale.stderr

    # a non-FE task branch never demands the browser receipt
    subprocess.run(
        ["git", "switch", "-qc", "task/B1"], cwd=toy_repo, check=True, capture_output=True
    )
    nonfe = _run([BASH, GATE_SCRIPT], toy_repo, stdin=f"{_PUSH} -u origin task/B1", extra_env=env)
    assert nonfe.returncode == 0, f"non-FE branch demanded a browser receipt: {nonfe.stderr}"


def test_pr_create_engages_the_gate_too(toy_repo: Path) -> None:
    """Codex #64 (class 9 — every constructor of the effect): `gh pr create`
    can push an unpushed branch itself, so a payload creating a PR must run
    the SAME gate as the push verb — deny without the receipts, pass with
    them. Discriminating: the push-verb-only matcher exited 0 on this."""
    assert BASH is not None
    (toy_repo / ".gitignore").write_text(
        ".claude/receipts/\norigin.git/\nshim/\nshot.png\n", encoding="utf-8"
    )
    _origin_and_branch(toy_repo, "task/FE2")
    (toy_repo / "a.md").write_text("fe edit via pr\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "fe"], cwd=toy_repo, check=True, capture_output=True)
    env = _uv_shim(toy_repo)

    denied = _run(
        [BASH, GATE_SCRIPT], toy_repo, stdin=f"{_PR_CREATE} --fill --base main", extra_env=env
    )
    assert denied.returncode == 2, f"PR creation bypassed the gate: {denied.stdout}"
    assert "receipt" in denied.stderr

    shot = toy_repo / "shot.png"
    shot.write_bytes(b"\x89PNG fake-but-non-empty")
    _stamp(toy_repo)
    _browser_stamp(toy_repo, shot)
    allowed = _run(
        [BASH, GATE_SCRIPT], toy_repo, stdin=f"{_PR_CREATE} --fill --base main", extra_env=env
    )
    assert allowed.returncode == 0, f"gate blocked a fully-receipted PR creation: {allowed.stderr}"
