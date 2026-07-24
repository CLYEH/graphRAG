"""Why: governance-check.sh is CI's deterministic half of the loop protocol —
DR-002's version-bump ratchet and the TASKS.md checkoff lint. Its bash (git
plumbing over base...HEAD, per-format version extraction) is regression-prone
state machinery with no tests until now (H19; the watcher's H8/H13 rework is
the precedent). These EXECUTE the real script inside throwaway git repos with
a real origin remote, pinning both the gate direction (violations fail) and
the pass direction (legitimate changes don't — an over-eager gate trains
people to bypass it).
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
SCRIPT = (REPO_ROOT / "scripts" / "governance-check.sh").as_posix()

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

YAML_V1 = 'info:\n  title: x\n  version: "1.0"\npaths: {}\n'
YAML_V2 = 'info:\n  title: x\n  version: "1.1"\npaths: {}\n'
JSON_V1 = '{"properties": {"schema_version": {"const": "1.0"}}}\n'
JSON_V2 = '{"properties": {"schema_version": {"const": "1.1"}}}\n'
# content change WITHOUT a version change — explicit constant, not a string
# replace: the first version used a .replace() whose needle didn't occur, so
# the "changed" file was byte-identical, the diff was empty, and the test
# asserted on a vacuous pass (caught by CI, where jq runs this path)
JSON_V1_TOUCHED = '{"properties": {"schema_version": {"const": "1.0"}, "x": {}}}\n'
TASKS_BASE = "# tasks\n- [ ] X1 first task\n- [ ] Y2 second task\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    """A work repo with a real `origin` (bare) holding branch main — the
    script runs `git fetch origin $BASE`, so the remote must exist."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    (work / "contracts").mkdir()
    (work / "contracts" / "openapi.yaml").write_text(YAML_V1, encoding="utf-8")
    (work / "contracts" / "mcp_response.schema.json").write_text(JSON_V1, encoding="utf-8")
    (work / "TASKS.md").write_text(TASKS_BASE, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work


def _run_check(work: Path, branch: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["BASE"] = "main"
    env["BRANCH"] = branch
    return subprocess.run(
        [str(BASH), SCRIPT],
        cwd=work,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )


def _branch_commit(work: Path, branch: str, files: dict[str, str | None]) -> None:
    _git(work, "checkout", "-q", "-b", branch)
    for rel, content in files.items():
        if content is None:
            _git(work, "rm", "-q", rel)
        else:
            (work / rel).write_text(content, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "change")


def test_contract_change_with_version_bump_passes(tmp_path: Path) -> None:
    work = _make_repo(tmp_path)
    _branch_commit(
        work,
        "task/X1",
        {
            "contracts/openapi.yaml": YAML_V2,
            "TASKS.md": TASKS_BASE.replace("- [ ] X1", "- [x] X1"),
        },
    )
    result = _run_check(work, "task/X1")
    assert result.returncode == 0, result.stdout + result.stderr


def test_contract_change_without_bump_fails(tmp_path: Path) -> None:
    """DR-002's whole point: editing frozen contract TEXT while the version
    VALUE stands still must gate — a token-level diff grep can be faked by a
    cosmetic edit; the value comparison can't."""
    work = _make_repo(tmp_path)
    _branch_commit(
        work,
        "task/X1",
        {
            "contracts/openapi.yaml": YAML_V1.replace("paths: {}", "paths: {x: {}}"),
            "TASKS.md": TASKS_BASE.replace("- [ ] X1", "- [x] X1"),
        },
    )
    result = _run_check(work, "task/X1")
    assert result.returncode == 1
    assert "::error" in result.stdout
    assert "version stayed" in result.stdout


def test_json_contract_uses_the_schema_version_const_field(tmp_path: Path) -> None:
    """The mcp schema's bump lives in `schema_version.const` — the H20a-era
    gap: a yaml-only version extractor reads nothing there and would either
    always-pass or always-fail json contracts."""
    if shutil.which("jq") is None:
        pytest.skip("jq not available")
    work = _make_repo(tmp_path)
    _branch_commit(
        work,
        "task/X1",
        {
            "contracts/mcp_response.schema.json": JSON_V1_TOUCHED,
            "TASKS.md": TASKS_BASE.replace("- [ ] X1", "- [x] X1"),
        },
    )
    result = _run_check(work, "task/X1")
    assert result.returncode == 1
    assert "version stayed" in result.stdout

    # and the bump direction passes
    work2 = _make_repo(tmp_path / "second")
    _branch_commit(
        work2,
        "task/X1",
        {
            "contracts/mcp_response.schema.json": JSON_V2,
            "TASKS.md": TASKS_BASE.replace("- [ ] X1", "- [x] X1"),
        },
    )
    result2 = _run_check(work2, "task/X1")
    assert result2.returncode == 0, result2.stdout + result2.stderr


def test_deleting_a_frozen_contract_fails(tmp_path: Path) -> None:
    """Contracts are frozen artifacts — deletion/rename is never a silent
    diff, whatever the version fields say."""
    work = _make_repo(tmp_path)
    _branch_commit(
        work,
        "task/X1",
        {
            "contracts/openapi.yaml": None,
            "TASKS.md": TASKS_BASE.replace("- [ ] X1", "- [x] X1"),
        },
    )
    result = _run_check(work, "task/X1")
    assert result.returncode == 1
    assert "deleted/renamed" in result.stdout


def test_task_branch_must_check_off_its_own_item(tmp_path: Path) -> None:
    work = _make_repo(tmp_path)
    _branch_commit(work, "task/X1", {"a.txt": "hi\n"})  # no TASKS.md checkoff
    result = _run_check(work, "task/X1")
    assert result.returncode == 1
    assert "must check off item 'X1'" in result.stdout


def test_smuggled_extra_checkoffs_fail(tmp_path: Path) -> None:
    """Exactly-own-item: checking off a SIBLING task in this PR would mark
    work done that no reviewer of this diff ever saw."""
    work = _make_repo(tmp_path)
    _branch_commit(
        work,
        "task/X1",
        {
            "TASKS.md": TASKS_BASE.replace("- [ ] X1", "- [x] X1").replace("- [ ] Y2", "- [x] Y2"),
        },
    )
    result = _run_check(work, "task/X1")
    assert result.returncode == 1
    assert "other than its own item" in result.stdout


def test_non_task_branch_skips_the_checkoff_lint(tmp_path: Path) -> None:
    """docs/* (the doc fast lane) and other non-task branches carry no
    checkoff obligation — the lint engaging there would block every retro."""
    work = _make_repo(tmp_path)
    _branch_commit(work, "docs/retro", {"note.md": "hi\n"})
    result = _run_check(work, "docs/retro")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checkoff lint skipped" in result.stdout


def test_reformatting_a_settled_entry_is_not_a_smuggled_checkoff(tmp_path: Path) -> None:
    """H22: the checkoff lint judges STATE, not diff lines.

    The retro's archive step rewrites already-checked entries (moving their
    as-built out), which a diff-line reading counts as dozens of foreign
    checkoffs — the migration PR hit exactly that. Nothing else in this file
    pins the distinction, so a revert to `+- [x]` counting would pass all the
    other tests while blocking every future retro.
    """
    work = _make_repo(tmp_path)
    settled = TASKS_BASE.replace("- [ ] Y2 second task", "- [x] Y2 second task — long as-built")
    (work / "TASKS.md").write_text(settled, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "Y2 done")
    _git(work, "push", "-q", "origin", "main")

    _branch_commit(
        work,
        "task/X1",
        {
            "TASKS.md": settled.replace("- [ ] X1 first task", "- [x] X1 first task").replace(
                "- [x] Y2 second task — long as-built", "- [x] Y2 second task"
            ),
        },
    )
    result = _run_check(work, "task/X1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "other than its own item" not in result.stdout


def test_an_over_ceiling_completed_entry_fails_the_gate(tmp_path: Path) -> None:
    """H22 ceiling, and the only test that discriminates the CWD path choice.

    `archive_task.py` resolves TASKS.md from the CWD so the gate judges the
    repo under test. Anchor it to `__file__` instead and this is the sole test
    that goes red — every other one keeps passing while the gate silently
    inspects THIS checkout and can never fail: a green that means nothing.
    """
    work = _make_repo(tmp_path)
    bloated = TASKS_BASE.replace("- [ ] X1 first task", "- [x] X1 first task — " + "z" * 400)
    _branch_commit(work, "task/X1", {"TASKS.md": bloated})
    result = _run_check(work, "task/X1")
    assert result.returncode == 1
    assert "H22" in result.stdout
    assert "archive_task.py" in result.stdout
