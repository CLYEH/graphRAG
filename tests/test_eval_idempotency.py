"""Why: contract v1.2 promises the async eval endpoint is idempotent per
(build, golden-set fingerprint). The build_id rides in the request path; this
fingerprint is the OTHER half, and it is load-bearing: if it did not change when
the golden set / query policy changed, reusing an Idempotency-Key within the TTL
would replay a run scored against STALE inputs — a false-green eval gate. These
tests pin that any content change flips the fingerprint, that it is stable
otherwise, and that an unsafe project name can never read outside the root.
CFG1 moved the POLICY component from ``config.yaml`` bytes to the canonical
serialization of the registry block (``policy_fingerprint_bytes``) — the same
guarantees hold, sourced from the ONE SoR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.eval.idempotency import (
    eval_inputs_fingerprint,
    policy_fingerprint_bytes,
    read_and_fingerprint_eval_inputs,
)

_POLICY = policy_fingerprint_bytes({"query_policy": {"max_top_k": 5}})


def _project(root: Path, *, golden: str = "cases: []") -> None:
    proj = root / "demo"
    (proj / "eval").mkdir(parents=True)
    (proj / "eval" / "golden.yaml").write_text(golden, encoding="utf-8")


def test_fingerprint_is_stable_for_unchanged_inputs(tmp_path: Path) -> None:
    _project(tmp_path)
    first = eval_inputs_fingerprint(tmp_path, "demo", _POLICY)
    second = eval_inputs_fingerprint(tmp_path, "demo", _POLICY)
    assert first == second  # same bytes → same key → the duplicate run replays


def test_a_changed_golden_set_flips_the_fingerprint(tmp_path: Path) -> None:
    """The whole point: editing the golden set within the Idempotency-Key TTL must
    change the request hash, so a reused key does not replay the stale-scored run."""
    _project(tmp_path, golden="cases: []")
    before = eval_inputs_fingerprint(tmp_path, "demo", _POLICY)
    (tmp_path / "demo" / "eval" / "golden.yaml").write_text(
        "cases: [{question: q, expect: a}]", encoding="utf-8"
    )
    assert eval_inputs_fingerprint(tmp_path, "demo", _POLICY) != before


def test_a_changed_query_policy_flips_the_fingerprint(tmp_path: Path) -> None:
    """The query policy is an eval input too (the runner reads it) — a registry
    policy edit must not silently replay a stale-scored run (CFG1: the policy
    component is the canonical bytes of the registry block)."""
    _project(tmp_path)
    before = eval_inputs_fingerprint(tmp_path, "demo", _POLICY)
    changed = policy_fingerprint_bytes({"query_policy": {"max_top_k": 9}})
    assert eval_inputs_fingerprint(tmp_path, "demo", changed) != before


def test_policy_bytes_are_canonical_and_fail_closed() -> None:
    """The SAME policy must hash identically however the dict arrived (key
    order / whitespace never flip the digest — accept-time and worker would
    otherwise false-drift on identical policies), and a missing/malformed
    block folds to empty bytes (stable; the eval JOB is the loud path that
    refuses to run without a valid policy)."""
    a = policy_fingerprint_bytes({"query_policy": {"a": 1, "b": 2}})
    b = policy_fingerprint_bytes({"query_policy": {"b": 2, "a": 1}})
    assert a == b
    assert policy_fingerprint_bytes({}) == b""
    assert policy_fingerprint_bytes(None) == b""
    assert policy_fingerprint_bytes({"query_policy": "not-a-mapping"}) == b""
    # presence vs absence of the block is a REAL change — must flip
    assert a != b""


def test_adding_a_missing_input_flips_the_fingerprint(tmp_path: Path) -> None:
    """A missing file contributes empty bytes, so ADDING the golden set (absent →
    present) changes the fingerprint — the first real golden set must not collide
    with the no-golden-set state."""
    proj = tmp_path / "demo"
    (proj / "eval").mkdir(parents=True)  # no golden yet
    absent = eval_inputs_fingerprint(tmp_path, "demo", _POLICY)
    (proj / "eval" / "golden.yaml").write_text("cases: []", encoding="utf-8")
    assert eval_inputs_fingerprint(tmp_path, "demo", _POLICY) != absent


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
    readable = eval_inputs_fingerprint(tmp_path, "demo", _POLICY)

    def fingerprint_when(method: str) -> str:
        """Fingerprint with ``Path.<method>`` raising PermissionError on golden.yaml."""
        real = getattr(Path, method)

        def boom(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == "golden.yaml":
                raise PermissionError(self.name)  # OSError subclass
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, boom)
        try:
            return eval_inputs_fingerprint(tmp_path, "demo", _POLICY)  # must not raise
        finally:
            monkeypatch.setattr(Path, method, real)

    read_fail = fingerprint_when("read_bytes")  # the file present but unreadable
    stat_fail = fingerprint_when("is_file")  # a parent dir non-searchable
    assert read_fail == stat_fail  # both siblings fold to the SAME sentinel state
    assert read_fail == fingerprint_when("read_bytes")  # stable → an Idem-Key replays
    assert read_fail != readable  # sentinel ≠ the file's real bytes
    # …and distinct from a MISSING golden set (unreadable ≠ absent → empty bytes).
    (tmp_path / "demo" / "eval" / "golden.yaml").unlink()
    assert read_fail != eval_inputs_fingerprint(tmp_path, "demo", _POLICY)


def test_unsafe_project_name_folds_to_a_stable_sentinel(tmp_path: Path) -> None:
    """A traversing project name ('..') must never read outside the projects root
    to build the hash — it folds to a stable sentinel (the eval job refuses the
    name the same way). Distinct from any real project's hash, stable across calls."""
    assert eval_inputs_fingerprint(tmp_path, "..", _POLICY) == "unsafe-project"
    assert eval_inputs_fingerprint(tmp_path, "..", _POLICY) == eval_inputs_fingerprint(
        tmp_path, "..", _POLICY
    )


def test_worker_read_matches_the_accept_time_fingerprint(tmp_path: Path) -> None:
    """The drift guard compares the PIN (built by ``eval_inputs_fingerprint`` at accept
    time) against the worker's LIVE read (``read_and_fingerprint_eval_inputs`` at
    dispatch). If the two hashing paths disagreed on IDENTICAL content, EVERY pinned
    eval would false-fail as 'drifted' — the load-bearing parity. Also pins that the
    returned golden bytes are the file's raw bytes: those exact bytes are what the
    worker then PARSES, so the fingerprint and the score can't diverge (triage 35);
    the policy side is parity-by-construction — BOTH paths hash the caller's single
    ``policy_fingerprint_bytes`` serialization of the same registry read."""
    _project(tmp_path, golden="cases: []")
    accept = eval_inputs_fingerprint(tmp_path, "demo", _POLICY)
    root = tmp_path / "demo"
    live, golden_bytes = read_and_fingerprint_eval_inputs(root, _POLICY)
    assert live == accept  # same content → same digest → a pinned eval reads unchanged
    assert golden_bytes == (root / "eval" / "golden.yaml").read_bytes()


def test_worker_read_raises_loud_on_a_missing_input(tmp_path: Path) -> None:
    """The accept-time fingerprint is TOLERANT (a missing/unreadable input folds to empty
    bytes / a sentinel) because it runs in the API BEFORE any job exists — raising would
    500 the request and create no watchable job. The worker read is the opposite: it runs
    inside the preflight that HAS a job to terminalize, so a missing golden set raises
    OSError and the preflight fails the job LOUD, never fingerprinting-then-parsing
    empty bytes."""
    root = tmp_path / "demo"
    (root / "eval").mkdir(parents=True)  # golden.yaml missing
    with pytest.raises(OSError):
        read_and_fingerprint_eval_inputs(root, _POLICY)
