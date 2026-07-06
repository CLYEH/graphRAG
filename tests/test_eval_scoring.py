"""Why: scoring turns golden expectations into the §20 gate's numbers — a
mapping bug silently inflates or deflates every build's score. Each function
gets its value cells, and the at-threshold comparison uses ADVERSARIAL
decimals (the §27.5/#12 boundary lesson: 0.8-0.5 > 0.3 in floats)."""

from __future__ import annotations

from core.eval import scoring
from core.query.results import McpResponse, RetrievalResult, SourceRef


def _result(
    result_type: str = "chunk",
    title: str | None = None,
    text: str | None = None,
    refs: tuple[SourceRef, ...] = (SourceRef(source_type="chunk", id="c-1"),),
) -> RetrievalResult:
    return RetrievalResult(
        result_type=result_type, id="r-1", score=0.5, source_refs=refs, title=title, text=text
    )


def _response(*results: RetrievalResult) -> McpResponse:
    return McpResponse(
        query="q",
        tool="semantic_search",
        project="p",
        build_id="b",
        results=tuple(results),
        warnings=(),
    )


def test_entity_recall_is_casefolded_containment() -> None:
    response = _response(_result(text="ACME partnered with Globex."))
    assert scoring.entity_recall(response, ["acme", "globex", "initech"]) == 2 / 3


def test_source_recall_matches_uri_or_id() -> None:
    response = _response(
        _result(
            refs=(
                SourceRef(source_type="chunk", id="c-1", source_uri="s3://docs/a.txt"),
                SourceRef(source_type="row", id="orders:42"),
            )
        )
    )
    assert scoring.source_recall(response, ["s3://docs/a.txt", "orders:42", "gone"]) == 2 / 3


def test_answer_regex_searches_all_visible_text() -> None:
    response = _response(_result(title="Q3 report", text="revenue rose 12%"))
    assert scoring.answer_regex_score(response, r"revenue.*\d+%") == 1.0
    assert scoring.answer_regex_score(response, r"loss") == 0.0
    # ORIGINAL casing: a case-sensitive golden pattern must match the text
    # as returned, not a casefolded copy ("Q3" would never match "q3")
    assert scoring.answer_regex_score(response, r"Q3 report") == 1.0
    assert scoring.answer_regex_score(response, r"q3 REPORT") == 0.0


def test_relation_hit_needs_all_three_in_one_result() -> None:
    """src+type+dst scattered across DIFFERENT results is not a hit — the
    expectation is one relation, not three co-occurrences."""
    split = _response(
        _result(result_type="relation", title="Acme partners_with Initech"),
        _result(result_type="relation", title="Globex acquires Umbrella"),
    )
    expected = [{"src": "Acme", "type": "partners_with", "dst": "Globex"}]
    assert scoring.relation_hit_rate(split, expected) == 0.0
    joined = _response(_result(result_type="relation", title="Acme partners_with Globex"))
    assert scoring.relation_hit_rate(joined, expected) == 1.0
    # §27.3: direction matters — the REVERSED edge must not satisfy src→dst
    reversed_edge = _response(_result(result_type="relation", title="Globex partners_with Acme"))
    assert scoring.relation_hit_rate(reversed_edge, expected) == 0.0
    # a PATH rendering a backward hop puts src textually before dst with a
    # "<-" arrow between them (graph.py's renderer) — still not a hit
    reversed_hop = _response(_result(result_type="path", title="Acme <-[partners_with]- Globex"))
    assert scoring.relation_hit_rate(reversed_hop, expected) == 0.0
    # ...but a genuine forward hop in a longer path IS one
    forward_hop = _response(
        _result(result_type="path", title="Initech -[owns]-> Acme -[partners_with]-> Globex")
    )
    assert scoring.relation_hit_rate(forward_hop, expected) == 1.0


def test_relation_hits_only_count_relation_and_path_results() -> None:
    chunk_mention = _response(_result(result_type="chunk", text="Acme partners_with Globex"))
    assert (
        scoring.relation_hit_rate(
            chunk_mention, [{"src": "Acme", "type": "partners_with", "dst": "Globex"}]
        )
        == 0.0
    )


def test_groundedness_is_the_cited_share() -> None:
    assert scoring.groundedness(_response()) == 0.0  # no results ⇒ nothing grounded
    assert scoring.groundedness(_response(_result(), _result())) == 1.0


def test_case_passed_tolerates_exact_threshold_adversarially() -> None:
    """0.8 - 0.5 = 0.30000000000000004; a case scoring 'exactly' min_score
    via inexact float arithmetic must PASS (the #12 lesson — adversarial
    decimals, not lucky ones)."""
    score = 0.8 - 0.5  # intended 0.3
    assert scoring.case_passed(score, 0.3)
    assert scoring.case_passed(0.3, 0.3)
    assert not scoring.case_passed(0.2999, 0.3)


def test_score_case_assembles_present_assertions_only() -> None:
    response = _response(_result(text="acme"))
    subscores = scoring.score_case(
        response,
        {"must_contain_entities": ["acme"], "groundedness_min": 0.5},
        path_validity_score=None,
    )
    assert subscores == {"entity_recall": 1.0, "groundedness": 1.0}
    assert scoring.case_score(subscores) == 1.0


def test_score_case_threads_path_validity_from_the_runner() -> None:
    response = _response(_result())
    subscores = scoring.score_case(
        response, {"must_contain_entities": ["gone"]}, path_validity_score=0.5
    )
    assert subscores["path_validity"] == 0.5
    assert subscores["entity_recall"] == 0.0


def test_score_case_covers_regex_and_relations_branches() -> None:
    response = _response(
        _result(result_type="relation", title="Acme partners_with Globex", text="acme deal")
    )
    subscores = scoring.score_case(
        response,
        {
            "answer_regex": "deal",
            "must_cite_sources": ["c-1"],
            "must_include_relations": [{"src": "Acme", "type": "partners_with", "dst": "Globex"}],
        },
        path_validity_score=None,
    )
    assert subscores == {
        "answer_regex": 1.0,
        "source_recall": 1.0,
        "citation_coverage": 1.0,  # the same recall, both frozen names emitted
        "relation_hit_rate": 1.0,
    }
