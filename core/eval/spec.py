"""Eval contract vocabulary + regression rule (DESIGN §20/§27.5, Track 0 P4).

Freezes what golden-set evaluation talks about:

1. **Metric names** — §20's scoring set plus §27.5's GraphRAG-specific
   additions. The eval runner (C10), ``EvalReport.metrics`` payloads (§15)
   and the Health trend UI (§19) all key results by these exact names, so a
   rename here silently forks the eval surface between producers and readers.
2. **Expects vocabulary** — the assertion fields a golden.yaml case may carry.
   ``contracts/golden.schema.json`` is the machine-checked source of truth;
   this tuple keeps Python-side consumers in lockstep with the schema file
   (a contract test asserts they match).
3. **Regression rule** (§20) — a candidate build regresses when its eval
   score falls below the active build's by *more than* the threshold. The
   threshold value is tunable (🔧 ``eval.regression_threshold``); the
   comparison direction is contract: activation preflight (§14) and Health's
   ``Eval regression`` light (§19) must agree on what "regression" means.
"""

from __future__ import annotations

#: §20 scoring: entity/source recall, answer similarity, citation coverage.
CORE_METRICS = ("entity_recall", "source_recall", "answer_similarity", "citation_coverage")

#: §27.5 GraphRAG-specific additions.
GRAPHRAG_METRICS = ("path_validity", "relation_hit_rate", "groundedness")

#: The full frozen metric vocabulary. Evolution is additive (DR-002).
METRICS = CORE_METRICS + GRAPHRAG_METRICS

#: Assertion fields of a golden case's ``expects`` block (§20 + §27.5) —
#: mirrors the Expects properties in contracts/golden.schema.json.
EXPECTS_FIELDS = (
    "must_contain_entities",
    "must_cite_sources",
    "answer_regex",
    "must_include_relations",
    "must_have_valid_paths",
    "groundedness_min",
)


#: Scores live in [0, 1], so an absolute tolerance is meaningful. Float
#: subtraction of decimal fractions is inexact (0.8 - 0.5 > 0.3), and without
#: slack an exactly-at-threshold drop — which the contract tolerates — would
#: read as a regression for the unlucky combinations.
_SCORE_TOLERANCE = 1e-9


def is_eval_regression(candidate: float, active: float, threshold: float) -> bool:
    """§20: does ``candidate``'s eval score regress against ``active``'s?

    True iff the candidate falls below the active build's score by more than
    ``threshold`` — dropping by exactly the threshold is still within
    tolerance (up to float slack, ``_SCORE_TOLERANCE``). A regression blocks
    auto-activate (§14 preflight) and lights ``Eval regression`` on Health
    (§19).
    """
    if threshold < 0:
        raise ValueError(f"regression threshold must be >= 0, got {threshold!r}")
    return (active - candidate) > threshold + _SCORE_TOLERANCE
