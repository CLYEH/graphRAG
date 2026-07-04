"""Why: resolution's pure parts carry §7's semantics — the block key must
never separate pairs it names (same type + shared token/prefix), thresholds
are 🟡 config whose invalid orderings must fail loudly (auto < review would
silently disable candidates), and canonical selection must be DETERMINISTIC
(more mentions → earlier created_at → smaller id) or re-runs flip-flop
canonicals and every downstream signature with them. The blocking/scoring
normalization is the SAME frozen rule identities are minted with
(fingerprints.norm_text) — a second implementation would be class-5 drift.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core.resolve.fingerprints import entity_key, norm_text
from core.resolve.resolution import (
    ResolutionConfig,
    _blocked_pairs,
    _Entity,
    _pick_canonical,
    _string_score,
)


def _e(
    name: str,
    *,
    etype: str = "Company",
    mentions: int = 0,
    created: str = "2026-07-01T00:00:00+00:00",
) -> _Entity:
    return _Entity(
        id=uuid.uuid4(),
        type=etype,
        name=name,
        entity_key=entity_key(etype, name),
        created_at=datetime.fromisoformat(created).astimezone(UTC),
        mention_count=mentions,
    )


def test_blocking_pairs_same_type_by_token_or_prefix_only() -> None:
    """The §7 block key: 'Acme Corp' & 'ACME Corporation' share the token/
    prefix 'acme' → paired; a different type or an unrelated name → never
    scored (blocking exists to make O(n²) scoring unnecessary, not to lose
    real matches)."""
    acme1 = _e("Acme Corp")
    acme2 = _e("ACME Corporation")
    person = _e("Acme Corp", etype="Person")  # same name, different type
    other = _e("Globex")
    pairs = _blocked_pairs([acme1, acme2, person, other])
    assert {(a.name, b.name) for a, b in pairs} == {("Acme Corp", "ACME Corporation")}


def test_scoring_uses_the_frozen_normalization() -> None:
    """Cosmetic variance (case/width/whitespace) must not depress the score —
    the comparison runs over norm_text, the SAME rule entity_key mints with."""
    assert _string_score(_e("ACME  Corp"), _e("acme corp")) == 1.0
    assert norm_text("ACME  Corp") == "acme corp"


def test_canonical_choice_is_deterministic() -> None:
    """More mentions wins; then earlier created_at; then smaller id — argument
    order never matters, or re-runs would flip canonicals and re-mint every
    signature differently each pass."""
    busy = _e("Acme", mentions=5)
    quiet = _e("Acme Corp", mentions=1)
    assert _pick_canonical(busy, quiet) == (busy, quiet)
    assert _pick_canonical(quiet, busy) == (busy, quiet)

    old = _e("Acme", created="2026-06-01T00:00:00+00:00")
    new = _e("Acme Corp", created="2026-07-01T00:00:00+00:00")
    assert _pick_canonical(new, old)[0] is old  # tie on mentions → earlier wins


def test_threshold_config_rejects_impossible_orderings() -> None:
    """auto < review would make the candidate band empty while LOOKING
    configured; embedding_weight=1.0 would zero the only live signal."""
    with pytest.raises(ValueError, match="thresholds"):
        ResolutionConfig(auto_merge_threshold=0.5, review_threshold=0.8)
    with pytest.raises(ValueError, match="embedding_weight"):
        ResolutionConfig(embedding_weight=1.0)
    assert ResolutionConfig().embedding_weight == 0.0  # 🔧 live when C5 wires vectors
