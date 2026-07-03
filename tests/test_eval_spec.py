"""Why: the eval gate protects activation (§14 preflight) and Health's trend
line (§19), and it only works if every producer/consumer speaks the same
vocabulary — the runner (C10) emits metric names, EvalReport.metrics carries
them, the Console plots them. §27.5 additionally freezes the GraphRAG-specific
trio; and the §20 regression rule must mean the same thing to preflight and
Health, or a build one side calls regressed the other calls fine.
"""

from __future__ import annotations

import pytest

from core.eval.spec import (
    CORE_METRICS,
    GRAPHRAG_METRICS,
    METRICS,
    is_eval_regression,
)

# --- metric vocabulary (§20/§27.5) --------------------------------------------


def test_metric_vocabulary_freezes_design() -> None:
    assert CORE_METRICS == (
        "entity_recall",
        "source_recall",
        "answer_similarity",
        "citation_coverage",
    )
    assert GRAPHRAG_METRICS == ("path_validity", "relation_hit_rate", "groundedness")
    assert METRICS == CORE_METRICS + GRAPHRAG_METRICS


def test_metric_names_are_unique() -> None:
    """EvalReport.metrics is a dict keyed by these names — a duplicate would
    make two metrics silently overwrite each other."""
    assert len(METRICS) == len(set(METRICS))


# --- regression rule (§20) ------------------------------------------------------


def test_drop_beyond_threshold_is_a_regression() -> None:
    assert is_eval_regression(candidate=0.79, active=0.90, threshold=0.10)


def test_drop_of_exactly_the_threshold_is_tolerated() -> None:
    """§20: 低於 active *超* 門檻 — the threshold is the allowed slack, so a
    drop of exactly the threshold must not block activation, or the tunable
    would mean 'threshold minus epsilon' and every consumer would compensate
    differently."""
    assert not is_eval_regression(candidate=0.80, active=0.90, threshold=0.10)


def test_equal_or_improved_score_is_never_a_regression() -> None:
    assert not is_eval_regression(candidate=0.90, active=0.90, threshold=0.10)
    assert not is_eval_regression(candidate=0.95, active=0.90, threshold=0.10)


def test_zero_threshold_blocks_any_drop() -> None:
    """threshold=0 is the strictest legal setting: any drop regresses, equal
    scores still pass."""
    assert is_eval_regression(candidate=0.899, active=0.90, threshold=0.0)
    assert not is_eval_regression(candidate=0.90, active=0.90, threshold=0.0)


def test_negative_threshold_fails_loud() -> None:
    """A negative threshold would call an *unchanged* score a regression —
    that's a config bug, not a tolerance, so it must not be silently applied."""
    with pytest.raises(ValueError):
        is_eval_regression(candidate=0.90, active=0.90, threshold=-0.01)
