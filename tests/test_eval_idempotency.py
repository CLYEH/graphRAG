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

import pytest

from core.eval.idempotency import (
    eval_inputs_fingerprint,
    read_and_fingerprint_eval_inputs,
)


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


def test_unreadable_input_does_not_abort_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why: the fingerprint is computed in the API BEFORE the eval job exists (when an
    Idempotency-Key is present). A present-but-unreadable golden set must NOT propagate
    — else the idempotent request 500s and creates NO watchable job, bypassing the
    worker preflight that terminalizes eval-input errors as a failed job. BOTH sibling
    filesystem calls in the hash loop can raise ``PermissionError`` on bad perms and
    both must fold to the SAME stable sentinel: ``read_bytes`` (the file itself) AND
    ``is_file`` (a non-searchable parent dir — is_file re-raises everything but
    not-found). So acceptance still succeeds and the JOB stays the sole loud path."""
    _project(tmp_path)
    readable = eval_inputs_fingerprint(tmp_path, "demo")

    def fingerprint_when(method: str) -> str:
        """Fingerprint with ``Path.<method>`` raising PermissionError on golden.yaml."""
        real = getattr(Path, method)

        def boom(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == "golden.yaml":
                raise PermissionError(self.name)  # OSError subclass
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, boom)
        try:
            return eval_inputs_fingerprint(tmp_path, "demo")  # must not raise
        finally:
            monkeypatch.setattr(Path, method, real)

    read_fail = fingerprint_when("read_bytes")  # the file present but unreadable
    stat_fail = fingerprint_when("is_file")  # a parent dir non-searchable
    assert read_fail == stat_fail  # both siblings fold to the SAME sentinel state
    assert read_fail == fingerprint_when("read_bytes")  # stable → an Idem-Key replays
    assert read_fail != readable  # sentinel ≠ the file's real bytes
    # …and distinct from a MISSING golden set (unreadable ≠ absent → empty bytes).
    (tmp_path / "demo" / "eval" / "golden.yaml").unlink()
    assert read_fail != eval_inputs_fingerprint(tmp_path, "demo")


def test_unsafe_project_name_folds_to_a_stable_sentinel(tmp_path: Path) -> None:
    """A traversing project name ('..') must never read <projects_dir>/../config.yaml
    to build the hash — it folds to a stable sentinel (the eval job refuses the name
    the same way). Distinct from any real project's hash, and stable across calls."""
    assert eval_inputs_fingerprint(tmp_path, "..") == "unsafe-project"
    assert eval_inputs_fingerprint(tmp_path, "..") == eval_inputs_fingerprint(tmp_path, "..")


def test_worker_read_matches_the_accept_time_fingerprint(tmp_path: Path) -> None:
    """The drift guard compares the PIN (built by ``eval_inputs_fingerprint`` at accept
    time) against the worker's LIVE read (``read_and_fingerprint_eval_inputs`` at
    dispatch). If the two hashing paths disagreed on IDENTICAL bytes, EVERY pinned eval
    would false-fail as 'drifted' — the load-bearing parity. Also pins that the returned
    bytes are the files' raw bytes: those exact bytes are what the worker then PARSES, so
    the fingerprint and the score can't diverge (the TOCTOU triage 35 closes)."""
    _project(tmp_path, golden="cases: []", config="policy: {top_k: 5}")
    accept = eval_inputs_fingerprint(tmp_path, "demo")
    root = tmp_path / "demo"
    live, golden_bytes, policy_bytes = read_and_fingerprint_eval_inputs(root)
    assert live == accept  # same content → same digest → a pinned eval reads as unchanged
    assert golden_bytes == (root / "eval" / "golden.yaml").read_bytes()
    assert policy_bytes == (root / "config.yaml").read_bytes()


def test_worker_read_raises_loud_on_a_missing_input(tmp_path: Path) -> None:
    """The accept-time fingerprint is TOLERANT (a missing/unreadable input folds to empty
    bytes / a sentinel) because it runs in the API BEFORE any job exists — raising would
    500 the request and create no watchable job. The worker read is the opposite: it runs
    inside the preflight that HAS a job to terminalize, so a missing input raises OSError
    and the preflight fails the job LOUD, never fingerprinting-then-parsing empty bytes."""
    root = tmp_path / "demo"
    (root / "eval").mkdir(parents=True)
    (root / "config.yaml").write_text("policy: {}", encoding="utf-8")  # golden.yaml missing
    with pytest.raises(OSError):
        read_and_fingerprint_eval_inputs(root)
