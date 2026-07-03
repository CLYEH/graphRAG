"""Why: review carry-forward (DR-003) only works if fingerprints are STABLE —
the same real-world thing must hash to the same key across builds, machines
and cosmetic variations, and different things must never collide. Likewise the
§17 state machines and §27.3 precedence are frozen contracts: resolve (C4)
will apply them blindly, so any drift here silently corrupts review decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.resolve.fingerprints import (
    FINGERPRINT_VERSION,
    entity_key,
    merge_key,
    relation_signature,
)
from core.resolve.review import (
    AUTO_DECIDER,
    LEDGER_DECISIONS,
    LEDGER_TARGET_KINDS,
    LedgerEntry,
    can_transition,
    effective_decision,
)

# --- fingerprints (§27.3) ----------------------------------------------------


def test_entity_key_is_deterministic_and_versioned() -> None:
    key = entity_key("Team", "People Ops")
    assert key == entity_key("Team", "People Ops")
    assert key.startswith(f"fpv{FINGERPRINT_VERSION}:")


def test_normalization_absorbs_cosmetic_variation() -> None:
    """Case, width (NFKC) and whitespace differences are the SAME entity —
    otherwise every rebuild would re-open already-reviewed items."""
    base = entity_key("Team", "People Ops")
    assert entity_key("team", "people   ops") == base
    assert entity_key("TEAM", "People Ops") == base  # NBSP → space via NFKC/split


def test_distinct_inputs_stay_distinct() -> None:
    assert entity_key("Team", "People Ops") != entity_key("Process", "People Ops")
    assert entity_key("Team", "People Ops") != entity_key("Team", "People Ops", "hr-42")


def test_part_boundaries_cannot_be_gamed() -> None:
    """Length-prefixed encoding: no separator smuggling can collide two
    different part tuples (the classic 'a|b','c' vs 'a','b|c' ambiguity)."""
    assert entity_key("a|b", "c") != entity_key("a", "b|c")


def test_disambiguator_is_trimmed_but_case_sensitive() -> None:
    with_id = entity_key("Team", "People Ops", " HR-42 ")
    assert with_id == entity_key("Team", "People Ops", "HR-42")
    assert with_id != entity_key("Team", "People Ops", "hr-42")  # external ids stay literal


def test_relation_signature_is_directional() -> None:
    a = entity_key("Team", "People Ops")
    b = entity_key("Process", "Onboarding")
    assert relation_signature(a, "OWNS", b) != relation_signature(b, "OWNS", a)
    assert relation_signature(a, "owns", b) == relation_signature(a, "OWNS", b)


def test_merge_key_is_symmetric() -> None:
    a = entity_key("Team", "People Ops")
    b = entity_key("Team", "PeopleOps")
    assert merge_key(a, b) == merge_key(b, a)


# --- state machines (§17) ----------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "current", "target", "allowed"),
    [
        ("entity_review", "unreviewed", "approved", True),
        ("entity_review", "unreviewed", "rejected", True),
        ("entity_review", "approved", "rejected", False),  # re-decisions go via the ledger
        ("relation_review", "unreviewed", "approved", True),
        ("merge_candidate", "pending", "deferred", True),
        ("merge_candidate", "deferred", "approved", True),
        ("merge_candidate", "approved", "pending", False),
        ("ontology_proposal", "proposed", "accepted", True),
        ("ontology_proposal", "accepted", "rejected", False),
    ],
)
def test_state_machine_freezes_section_17(
    kind: str, current: str, target: str, allowed: bool
) -> None:
    assert can_transition(kind, current, target) is allowed


def test_unknown_kind_fails_loud() -> None:
    with pytest.raises(ValueError):
        can_transition("document_review", "unreviewed", "approved")


def test_frozen_vocabularies_match_design() -> None:
    assert LEDGER_TARGET_KINDS == ("entity", "relation", "merge")
    assert LEDGER_DECISIONS == ("approve", "reject", "defer", "merge", "split")


# --- ledger precedence (§27.3) -------------------------------------------------


def _entry(decision: str, by: str, at: str, fpv: int = FINGERPRINT_VERSION) -> LedgerEntry:
    return LedgerEntry(decision, by, datetime.fromisoformat(at).replace(tzinfo=UTC), fpv)


def test_other_fingerprint_versions_never_apply() -> None:
    """DR-007: a key minted under different normalization rules must trigger
    re-review, not silent reuse."""
    stale = _entry("reject", "curator-1", "2026-07-01T00:00:00", fpv=FINGERPRINT_VERSION + 1)
    assert effective_decision([stale]) is None


def test_manual_outranks_newer_auto() -> None:
    manual = _entry("approve", "curator-1", "2026-07-01T00:00:00")
    auto = _entry("reject", AUTO_DECIDER, "2026-07-02T00:00:00")
    picked = effective_decision([auto, manual])
    assert picked is manual


def test_latest_decision_wins_within_rank() -> None:
    old = _entry("approve", "curator-1", "2026-07-01T00:00:00")
    new = _entry("reject", "curator-2", "2026-07-02T00:00:00")
    assert effective_decision([old, new]) is new


def test_auto_applies_when_no_manual_exists() -> None:
    auto = _entry("reject", AUTO_DECIDER, "2026-07-01T00:00:00")
    picked = effective_decision([auto])
    assert picked is auto
