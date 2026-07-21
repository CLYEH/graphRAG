"""Why: the stale-claims lint (H20d) is the mechanized half of lesson class 2
— a memory file claiming a task is still pending after TASKS.md checked it
off misleads the next session's prep read. The lint must fire on exactly the
checked-id × pending-marker CO-OCCURRENCE and nothing subtler (class 14: a
closed marker set plus ids enumerated from TASKS.md itself, not prose
parsing), and it must NEVER gate (always exit 0) — these run the real bash
script (class 7: infra must be executed to be reviewed).
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
SCRIPT = (REPO_ROOT / "scripts" / "memory-stale-claims.sh").as_posix()

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run(tasks: Path, memdir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [str(BASH), SCRIPT, tasks.as_posix(), memdir.as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def _fixture(tmp_path: Path, tasks_text: str, memory_text: str) -> tuple[Path, Path]:
    tasks = tmp_path / "TASKS.md"
    tasks.write_text(tasks_text, encoding="utf-8")
    memdir = tmp_path / "memory"
    memdir.mkdir()
    (memdir / "note.md").write_text(memory_text, encoding="utf-8")
    return tasks, memdir


def test_warns_when_a_checked_task_is_still_claimed_pending(tmp_path: Path) -> None:
    tasks, memdir = _fixture(
        tmp_path,
        "- [x] H21 web 測試 gate\n- [ ] H19 merge-gate 測試\n",
        "第一行沒事\n尚餘 H21 的 durable fix 未做\n",
    )
    res = _run(tasks, memdir)
    assert res.returncode == 0
    assert "::warning" in res.stdout
    assert "'H21'" in res.stdout
    assert "line=2" in res.stdout  # the annotation must point at the claiming LINE


def test_unchecked_task_never_warns(tmp_path: Path) -> None:
    # the claim is TRUE while the task is open — warning here would train
    # people to ignore the lint
    tasks, memdir = _fixture(
        tmp_path,
        "- [ ] H19 merge-gate 測試\n",
        "尚餘 H19 的 subprocess 測試未做\n",
    )
    res = _run(tasks, memdir)
    assert res.returncode == 0
    assert "::warning" not in res.stdout


def test_checked_id_without_a_pending_marker_never_warns(tmp_path: Path) -> None:
    # mentioning a finished task is normal history, not a stale claim
    tasks, memdir = _fixture(
        tmp_path,
        "- [x] H21 web 測試 gate\n",
        "H21 已完成,vite.config.ts 設了 maxWorkers。\n",
    )
    res = _run(tasks, memdir)
    assert res.returncode == 0
    assert "::warning" not in res.stdout


def test_id_match_respects_word_boundaries(tmp_path: Path) -> None:
    # checked H2 must NOT fire on an H20 line — a substring match would spray
    # false warnings across every sibling id family
    tasks, memdir = _fixture(
        tmp_path,
        "- [x] H2 codex 判讀政策\n",
        "尚餘 H20 系列還沒收完\n",
    )
    res = _run(tasks, memdir)
    assert res.returncode == 0
    assert "::warning" not in res.stdout


def test_digitless_checked_words_are_not_ids(tmp_path: Path) -> None:
    # early setup items check off generic words ("CI") — treating them as
    # task ids warns on every ordinary use of the word (first live run).
    # The discriminating PROPERTY: real task ids contain a digit.
    tasks, memdir = _fixture(
        tmp_path,
        "- [x] CI (workflows: backend + frontend)\n",
        "CI 綠了但尚未拿到 +1 之前不准 merge\n",
    )
    res = _run(tasks, memdir)
    assert res.returncode == 0
    assert "::warning" not in res.stdout


def test_pending_claim_in_a_DIFFERENT_clause_does_not_warn(tmp_path: Path) -> None:
    # a long index line legitimately reads "X 已 merge;尚餘 Y" — the pending
    # claim is about Y, and warning on X trains people to ignore the lint
    # (first live run caught exactly this on the gov-fe index line)
    tasks, memdir = _fixture(
        tmp_path,
        "- [x] GOV3-fe 本體提案審核\n",
        "GOV3-fe 已 merge(#104);尚餘 gap-list FE 片未立案\n",
    )
    res = _run(tasks, memdir)
    assert res.returncode == 0
    assert "::warning" not in res.stdout


def test_pending_claim_in_the_SAME_clause_still_warns(tmp_path: Path) -> None:
    # the clause split must not swallow true positives
    tasks, memdir = _fixture(
        tmp_path,
        "- [x] GOV3-fe 本體提案審核\n",
        "別的事已完成;GOV3-fe 尚未實作\n",
    )
    res = _run(tasks, memdir)
    assert res.returncode == 0
    assert res.stdout.count("::warning") == 1
    assert "GOV3-fe" in res.stdout


def test_exit_is_zero_even_with_warnings(tmp_path: Path) -> None:
    # 非 gate is the owner-decided contract: warnings inform, never block
    tasks, memdir = _fixture(
        tmp_path,
        "- [x] MCP1 前端顯示\n",
        "待實作 MCP1 的顯示面\n尚未 MCP1 收尾\n",
    )
    res = _run(tasks, memdir)
    assert res.returncode == 0
    assert res.stdout.count("::warning") == 2


def test_real_repo_state_is_currently_clean() -> None:
    """The lint against the ACTUAL repo must be quiet today — if this fails,
    either a memory file has a genuinely stale claim (fix the memory) or the
    lint has grown a false-positive pattern (fix the lint). Either way the
    signal-to-noise contract (warnings are rare and real) is what this pins.

    Known residual: a red here on a SHORT id (P2, C3, H8…) may be a
    priority-label/stage-name prose collision, not a stale claim — the
    resolution is reword-the-clause or an id exemption in the script, not
    deleting this test (see the script's KNOWN RESIDUAL note)."""
    res = _run(REPO_ROOT / "TASKS.md", REPO_ROOT / ".claude" / "memory")
    assert res.returncode == 0
    assert "::warning" not in res.stdout, res.stdout
