"""Why: contract v1.2 promises the async eval endpoint is idempotent per
(build, golden-set fingerprint). The build_id rides in the request path; this
fingerprint is the OTHER half, and it is load-bearing: if it did not change when
the golden set / query policy changed, reusing an Idempotency-Key within the TTL
would replay a run scored against STALE inputs — a false-green eval gate. These
tests pin that any content change flips the fingerprint, that it is stable
otherwise, and that an unsafe project name can never read outside the root.
"""

from __future__ import annotations

from pathlib import Path

from core.eval.idempotency import eval_inputs_fingerprint


def _project(root: Path, *, golden: str = "cases: []", config: str = "policy: {}") -> None:
    proj = root / "demo"
    (proj / "eval").mkdir(parents=True)
    (proj / "eval" / "golden.yaml").write_text(golden, encoding="utf-8")
    (proj / "config.yaml").write_text(config, encoding="utf-8")


def test_fingerprint_is_stable_for_unchanged_inputs(tmp_path: Path) -> None:
    _project(tmp_path)
    first = eval_inputs_fingerprint(tmp_path, "demo")
    second = eval_inputs_fingerprint(tmp_path, "demo")
    assert first == second  # same bytes → same key → the duplicate run replays


def test_a_changed_golden_set_flips_the_fingerprint(tmp_path: Path) -> None:
    """The whole point: editing the golden set within the Idempotency-Key TTL must
    change the request hash, so a reused key does not replay the stale-scored run."""
    _project(tmp_path, golden="cases: []")
    before = eval_inputs_fingerprint(tmp_path, "demo")
    (tmp_path / "demo" / "eval" / "golden.yaml").write_text(
        "cases: [{question: q, expect: a}]", encoding="utf-8"
    )
    assert eval_inputs_fingerprint(tmp_path, "demo") != before


def test_a_changed_query_policy_flips_the_fingerprint(tmp_path: Path) -> None:
    """The query policy (config.yaml) is an eval input too — the runner reads it, so
    it is part of the fingerprint; a policy edit must not silently replay."""
    _project(tmp_path, config="policy: {}")
    before = eval_inputs_fingerprint(tmp_path, "demo")
    (tmp_path / "demo" / "config.yaml").write_text("policy: {top_k: 5}", encoding="utf-8")
    assert eval_inputs_fingerprint(tmp_path, "demo") != before


def test_adding_a_missing_input_flips_the_fingerprint(tmp_path: Path) -> None:
    """A missing file contributes empty bytes, so ADDING the golden set (absent →
    present) changes the fingerprint — the first real golden set must not collide
    with the no-golden-set state."""
    proj = tmp_path / "demo"
    (proj / "eval").mkdir(parents=True)
    (proj / "config.yaml").write_text("policy: {}", encoding="utf-8")  # no golden yet
    absent = eval_inputs_fingerprint(tmp_path, "demo")
    (proj / "eval" / "golden.yaml").write_text("cases: []", encoding="utf-8")
    assert eval_inputs_fingerprint(tmp_path, "demo") != absent


def test_unsafe_project_name_folds_to_a_stable_sentinel(tmp_path: Path) -> None:
    """A traversing project name ('..') must never read <projects_dir>/../config.yaml
    to build the hash — it folds to a stable sentinel (the eval job refuses the name
    the same way). Distinct from any real project's hash, and stable across calls."""
    assert eval_inputs_fingerprint(tmp_path, "..") == "unsafe-project"
    assert eval_inputs_fingerprint(tmp_path, "..") == eval_inputs_fingerprint(tmp_path, "..")
