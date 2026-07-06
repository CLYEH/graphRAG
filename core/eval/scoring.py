"""Case scoring — pure functions over §16 responses (§20/§27.5; C10).

Every subscore is 0..1. The system returns RETRIEVAL results, not synthesized
prose, so §20's text-facing assertions score against the results' visible
text (titles + texts) and their citations (source_refs):

- ``must_contain_entities`` → entity_recall: the share of expected canonical
  names appearing (case-folded substring) in any result's title/text.
- ``must_cite_sources`` → source_recall AND citation_coverage: the share of
  expected source identifiers covered by the response's source_refs
  (matching ``source_uri`` or ``id``).
- ``answer_regex`` → 1/0: the pattern matches the concatenated result text
  in its ORIGINAL casing (case-sensitive, as regexes are by default).
- ``must_include_relations`` → relation_hit_rate: a relation expectation
  hits when the expected hop appears as one DELIMITED unit: in
  arrow-rendered text the node labels are bounded by the renderer's
  arrows/string edges, so "src -[type]-> dst" must match with both
  endpoint labels exact (§27.3 — substring endpoints, reversed hops,
  intermediate nodes, and other types on the direct hop do not hit);
  plain titles match src, type, dst in order on word boundaries.
- ``must_have_valid_paths`` → path_validity: the share of path results whose
  per-edge relation refs all resolve against the SoR (the runner passes the
  resolution callback); asserting it with NO path results returned scores 0
  — the mode was expected to produce paths.
- ``groundedness_min`` → binary: groundedness (share of results carrying at
  least one source_ref) meets the floor. The §16 contract already demands
  refs, so this guards projection/enrichment holes rather than prose claims.

``answer_similarity`` (§20) needs a reference answer, which the frozen golden
schema does not carry — the metric is NOT emitted (omitted, never faked);
adding a reference field to the schema is an additive evolution for later.

The case score is the MEAN of the present assertions' subscores (the schema
guarantees at least one); the case passes when score >= min_score (the
at-threshold tolerance lives in spec.is_eval_regression's domain — here a
plain >= on floats both derived from the same arithmetic is exact enough
because min_score comes verbatim from YAML, so we compare with the same
1e-9 slack used by the regression rule).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from core.eval.spec import SCORE_TOLERANCE
from core.query.results import McpResponse, RetrievalResult


def _raw_text(result: RetrievalResult) -> str:
    return f"{result.title or ''}\n{result.text or ''}"


def _visible_text(result: RetrievalResult) -> str:
    return _raw_text(result).casefold()


def _all_text(response: McpResponse) -> str:
    return "\n".join(_visible_text(result) for result in response.results)


def _all_raw_text(response: McpResponse) -> str:
    return "\n".join(_raw_text(result) for result in response.results)


def entity_recall(response: McpResponse, expected: list[str]) -> float:
    corpus = _all_text(response)
    hits = sum(1 for name in expected if name.casefold() in corpus)
    return hits / len(expected)


def source_recall(response: McpResponse, expected: list[str]) -> float:
    cited: set[str] = set()
    for result in response.results:
        for ref in result.source_refs:
            cited.add(ref.id)
            if ref.source_uri is not None:
                cited.add(ref.source_uri)
    hits = sum(1 for uri in expected if uri in cited)
    return hits / len(expected)


def answer_regex_score(response: McpResponse, pattern: str) -> float:
    # ORIGINAL casing: the golden pattern is an ordinary case-sensitive
    # regex — matching against casefolded text would fail valid answers
    # (authors can opt into insensitivity with (?i) themselves)
    return 1.0 if re.search(pattern, _all_raw_text(response)) else 0.0


def relation_hit_rate(response: McpResponse, expected: Sequence[Mapping[str, str]]) -> float:
    texts = [
        _visible_text(result)
        for result in response.results
        if result.result_type in ("relation", "path")
    ]
    hits = 0
    for expectation in expected:
        src = re.escape(expectation["src"].casefold())
        rel_type = re.escape(expectation["type"].casefold())
        dst = re.escape(expectation["dst"].casefold())
        # arrow-rendered text (paths and graph relation results): node labels
        # are DELIMITED by the renderer's arrows and the string boundaries,
        # so the whole expected hop must appear as one delimited unit —
        # "MegaAcme -[t]-> Globex" or "Acme -[t]-> GlobexCorp" must NOT hit
        # an expected Acme→Globex (endpoint labels are identities, not
        # substrings); reversed hops and intermediate nodes cannot match by
        # construction (§27.3).
        arrow = rf"(?:^|\]->\s|\]-\s){src}\s-\[{rel_type}\]->\s{dst}(?:$|\s-\[|\s<-\[)"
        # plain titles (no arrows): src, type, dst in order, each on its own
        # word boundary — an embedded label ("Acmeta") is a different name
        plain = rf"(?<!\w){src}(?!\w).*?(?<!\w){rel_type}(?!\w).*?(?<!\w){dst}(?!\w)"
        for text in texts:
            if "-[" in text:
                if re.search(arrow, text, re.MULTILINE):
                    hits += 1
                    break
            elif re.search(plain, text, re.DOTALL):
                hits += 1
                break
    return hits / len(expected)


def groundedness(response: McpResponse) -> float:
    if not response.results:
        return 0.0
    grounded = sum(1 for result in response.results if result.source_refs)
    return grounded / len(response.results)


def case_score(subscores: Mapping[str, float]) -> float:
    """Mean of the present assertions' subscores (schema: at least one)."""
    return sum(subscores.values()) / len(subscores)


def case_passed(score: float, min_score: float) -> bool:
    """>= with the shared score tolerance: an exactly-at-threshold case
    passes even when float arithmetic lands a hair under (the §20/§27.5
    boundary lesson — adversarial decimals, not lucky ones)."""
    return score >= min_score - SCORE_TOLERANCE


def score_case(
    response: McpResponse,
    expects: Mapping[str, Any],
    path_validity_score: float | None,
) -> dict[str, float]:
    """All subscores for one case. ``path_validity_score`` is computed by the
    RUNNER (it needs SoR access to resolve edge refs) and passed in; None
    means the case does not assert it."""
    subscores: dict[str, float] = {}
    if "must_contain_entities" in expects:
        subscores["entity_recall"] = entity_recall(response, expects["must_contain_entities"])
    if "must_cite_sources" in expects:
        recall = source_recall(response, expects["must_cite_sources"])
        subscores["source_recall"] = recall
        subscores["citation_coverage"] = recall
    if "answer_regex" in expects:
        subscores["answer_regex"] = answer_regex_score(response, expects["answer_regex"])
    if "must_include_relations" in expects:
        subscores["relation_hit_rate"] = relation_hit_rate(
            response, expects["must_include_relations"]
        )
    if path_validity_score is not None:
        subscores["path_validity"] = path_validity_score
    if "groundedness_min" in expects:
        value = groundedness(response)
        subscores["groundedness"] = (
            1.0 if value >= expects["groundedness_min"] - SCORE_TOLERANCE else 0.0
        )
    return subscores
