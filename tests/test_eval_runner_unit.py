"""Why: the runner's pure seams — graph-param derivation, mode dispatch,
path-ref resolution, the persisted payload shape — decide what the §20 gate
sees. These run without stores (fakes), keeping the fast gate's coverage on
the surface CI actually guards; the live wiring is proven in
test_eval_runner_integration.py."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.eval.golden import GoldenCase
from core.eval.runner import (
    CaseResult,
    EvalReport,
    _derive_graph_params,
    _path_validity,
    _run_case,
)
from core.query.results import McpResponse, RetrievalResult, SourceRef


def _case(mode: str, expects: dict[str, Any]) -> GoldenCase:
    return GoldenCase(question="q?", mode=mode, expects=expects, min_score=0.5)


def test_graph_params_cover_every_expectation() -> None:
    """score_case computes each metric over its WHOLE expectation list — any
    expectation the runner never fetches under-scores valid builds (Codex
    rounds 3+5): every relation gets a path query (connectivity) PLUS a
    1-hop src SUBGRAPH (shortest_path is untyped, and only subgraph emits
    rendered typed relation results), and every expected entity gets its own
    subgraph (it INCLUDES the seed — neighbors excludes it, so a singleton
    expected entity would score 0); duplicates are folded."""
    param_list = _derive_graph_params(
        _case(
            "graph",
            {
                "must_include_relations": [
                    {"src": "Acme", "type": "t", "dst": "Globex"},
                    {"src": "Globex", "type": "t2", "dst": "Initech"},
                ],
                # Initech rides no relation SRC; Umbrella is off-path entirely
                "must_contain_entities": ["Acme", "Umbrella"],
            },
        ),
        max_hops=3,
    )
    assert [(p.template, p.entity, p.other_entity, p.hops) for p in param_list] == [
        ("path", "Acme", "Globex", 3),
        ("subgraph", "Acme", None, 1),
        ("path", "Globex", "Initech", 3),
        ("subgraph", "Globex", None, 1),
        # "Acme" subgraph deduped — already derived from the first relation
        ("subgraph", "Umbrella", None, 1),
    ]


def test_graph_params_fall_back_to_subgraph_then_empty() -> None:
    param_list = _derive_graph_params(_case("graph", {"must_contain_entities": ["Acme"]}), 3)
    assert [(p.template, p.entity) for p in param_list] == [("subgraph", "Acme")]
    assert _derive_graph_params(_case("graph", {"answer_regex": "x"}), 3) == []


async def test_an_underivable_graph_case_scores_zero_loudly() -> None:
    """Rule 12: a case the runner cannot drive is a scored FAILURE with a
    note naming what to add — never a silent skip that inflates the gate."""
    policy = SimpleNamespace(max_graph_hops=3)
    response, note = await _run_case(
        cast(Any, None), cast(Any, policy), _case("graph", {"answer_regex": "x"})
    )
    assert response is None
    assert note is not None and "no derivable anchor" in note


def _path_result(*edge_ids: str) -> RetrievalResult:
    return RetrievalResult(
        result_type="path",
        id="p-1",
        score=0.5,
        source_refs=tuple(SourceRef(source_type="relation", id=edge_id) for edge_id in edge_ids),
    )


class _FakeRepo:
    def __init__(self, known: set[uuid.UUID]) -> None:
        self._known = known

    async def fetch_all(self, table: Any, *conditions: Any) -> list[Any]:
        return [SimpleNamespace(id=known_id) for known_id in self._known]


async def test_path_validity_resolves_edges_against_the_sor() -> None:
    real = uuid.uuid4()
    fake = uuid.uuid4()
    repo = _FakeRepo({real})
    response = McpResponse(
        query="q",
        tool="graph_query",
        project="p",
        build_id="b",
        results=(_path_result(str(real)), _path_result(str(fake)), _path_result("not-a-uuid")),
        warnings=(),
    )
    assert await _path_validity(cast(Any, repo), response) == pytest.approx(1 / 3)


async def test_path_validity_is_none_without_paths() -> None:
    response = McpResponse(
        query="q", tool="graph_query", project="p", build_id="b", results=(), warnings=()
    )
    assert await _path_validity(cast(Any, _FakeRepo(set())), response) is None


def test_metrics_payload_carries_what_the_gate_reads() -> None:
    report = EvalReport(
        build_id=uuid.uuid4(),
        score=0.75,
        passed=1,
        failed=1,
        cases=(
            CaseResult("q1", "semantic", 1.0, True, {"entity_recall": 1.0}),
            CaseResult("q2", "graph", 0.5, False, {}, note="no anchor"),
        ),
        metrics={"entity_recall": 1.0},
        fingerprint="fp-1",
    )
    payload = report.to_eval_payload()
    assert payload["score"] == 0.75  # the §14 gate compares exactly this
    assert payload["fingerprint"] == "fp-1"  # suite identity rides along
    assert payload["passed"] == 1 and payload["failed"] == 1
    assert [c["question"] for c in payload["cases"]] == ["q1", "q2"]


async def test_run_eval_dispatches_scores_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full runner loop on fakes: every mode dispatches to ITS function,
    subscores aggregate into the report (answer_regex excluded from the
    frozen metrics), and the payload is persisted via one UPDATE. The live
    path is proven in integration; this pins the seams the fast gate guards."""
    import core.eval.runner as runner_module
    from core.eval.golden import GoldenSet
    from core.eval.runner import run_eval

    binding = SimpleNamespace(project="p", build_id=uuid.uuid4())
    monkeypatch.setattr(
        runner_module, "resolve_eval_binding", lambda conn, project, build_id: _async(binding)
    )
    for cls_name in (
        "BuildScopedRepo",
        "BuildScopedVectorRepo",
        "BuildScopedSqlReader",
        "BuildScopedGraphRepo",
    ):
        monkeypatch.setattr(
            runner_module,
            cls_name,
            SimpleNamespace(bound_to=lambda *a, **k: SimpleNamespace()),
        )

    def _resp(tool: str, text: str) -> McpResponse:
        return McpResponse(
            query="q",
            tool=tool,
            project="p",
            build_id="b",
            results=(
                RetrievalResult(
                    result_type="chunk",
                    id="r",
                    score=0.5,
                    source_refs=(SourceRef(source_type="chunk", id="c-1"),),
                    text=text,
                ),
            ),
            warnings=(),
        )

    calls: list[str] = []

    def _mode(name: str, tool: str) -> Any:
        async def _fn(*args: Any, **kwargs: Any) -> McpResponse:
            calls.append(name)
            return _resp(tool, "acme text")

        return _fn

    monkeypatch.setattr(runner_module, "semantic_search", _mode("semantic", "semantic_search"))
    monkeypatch.setattr(runner_module, "sql_query", _mode("sql", "sql_query"))
    monkeypatch.setattr(runner_module, "global_summary", _mode("global", "global_summary"))
    monkeypatch.setattr(runner_module, "graph_query", _mode("graph", "graph_query"))
    monkeypatch.setattr(runner_module, "hybrid_query", _mode("hybrid", "hybrid_query"))
    monkeypatch.setattr(runner_module, "hybrid_policy", lambda *a, **k: SimpleNamespace())

    class _Txn:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Conn:
        def __init__(self) -> None:
            self.executed: list[Any] = []

        async def rollback(self) -> None:
            return None

        def begin(self) -> _Txn:
            return _Txn()

        async def execute(self, statement: Any) -> Any:
            self.executed.append(statement)
            return SimpleNamespace(rowcount=1)  # the persist finds its build

    conn = _Conn()
    policy = SimpleNamespace(
        top_k=lambda requested: 5,
        sql_rows=lambda: 10,
        sql_policy=lambda: SimpleNamespace(),
        cypher_policy=lambda: SimpleNamespace(),
        max_graph_hops=3,
    )
    golden = GoldenSet(
        cases=(
            _case("semantic", {"must_contain_entities": ["acme"], "answer_regex": "acme"}),
            _case("sql", {"must_contain_entities": ["acme"]}),
            _case("global", {"must_contain_entities": ["missing"]}),
            _case("graph", {"must_contain_entities": ["acme"]}),
            _case("hybrid", {"must_contain_entities": ["acme"]}),
        )
    )
    report = await run_eval(
        cast(Any, conn),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        "p",
        binding.build_id,
        golden,
        cast(Any, policy),
    )
    assert calls == ["semantic", "sql", "global", "graph", "hybrid"]
    assert report.passed == 4 and report.failed == 1  # the 'missing' case
    assert "answer_regex" not in report.metrics  # case assertion, not a metric
    assert report.metrics["entity_recall"] == pytest.approx(4 / 5)
    assert len(conn.executed) == 1  # ONE persisting UPDATE to builds.eval


def _async(value: Any) -> Any:
    async def _coro() -> Any:
        return value

    return _coro()


def test_merge_responses_dedupes_and_unions() -> None:
    """Fix-A's downstream: per-relation graph responses merge into ONE §16
    response for scoring — a result appearing in two responses counts once
    (dedupe by (result_type, id), first kept), warnings union by value in
    order (QueryWarning is a frozen dataclass — equality is by fields)."""
    from core.eval.runner import _merge_responses
    from core.query.results import QueryWarning

    def _resp(result_id: str, score: float, warning: str) -> McpResponse:
        return McpResponse(
            query="q",
            tool="graph_query",
            project="p",
            build_id="b",
            results=(
                RetrievalResult(
                    result_type="relation",
                    id=result_id,
                    score=score,
                    source_refs=(SourceRef(source_type="chunk", id="c-1"),),
                ),
            ),
            warnings=(QueryWarning("TRUNCATED", warning),),
        )

    merged = _merge_responses(
        [_resp("r-1", 0.9, "shared"), _resp("r-1", 0.1, "shared"), _resp("r-2", 0.5, "extra")]
    )
    assert [r.id for r in merged.results] == ["r-1", "r-2"]
    assert merged.results[0].score == 0.9  # the FIRST occurrence is kept
    assert [w.message for w in merged.warnings] == ["shared", "extra"]  # unioned once, in order


def test_eval_fingerprint_is_a_pure_function_of_the_suite() -> None:
    """§20 comparability: two reports may be compared only when scored
    against the SAME golden set + policy. The fingerprint must be stable
    for identical inputs and differ when the suite differs — otherwise a
    candidate scored on an easier suite passes on raw numbers."""
    import dataclasses
    from types import SimpleNamespace as _NS

    from core.eval.golden import GoldenSet
    from core.eval.runner import eval_fingerprint

    @dataclasses.dataclass(frozen=True)
    class _Policy:
        max_top_k: int = 20
        max_latency_ms: int = 10000

    golden_a = GoldenSet(cases=(_case("semantic", {"must_contain_entities": ["Acme"]}),))
    golden_b = GoldenSet(cases=(_case("semantic", {"must_contain_entities": ["Globex"]}),))
    models = {"llm_provider": "openai", "llm_model": "m1", "embedding_model": "e1"}
    fp1 = eval_fingerprint(golden_a, cast(Any, _Policy()), models)
    fp2 = eval_fingerprint(golden_a, cast(Any, _Policy()), models)
    fp3 = eval_fingerprint(golden_b, cast(Any, _Policy()), models)
    fp4 = eval_fingerprint(golden_a, cast(Any, _Policy(max_top_k=5)), models)
    fp5 = eval_fingerprint(golden_a, cast(Any, _Policy()), {**models, "llm_model": "m2"})
    assert fp1 == fp2  # deterministic
    assert fp1 != fp3  # different golden set → different identity
    assert fp1 != fp4  # different policy → different identity
    assert fp1 != fp5  # different MODEL → different identity (round 16)
    _ = _NS  # keep import local-scope tidy


def test_models_needed_maps_modes_to_clients() -> None:
    """A golden set must evaluate WITHOUT an API key whenever no case's mode
    would actually call a model UNDER THIS POLICY (Codex rounds 10+13): an
    sql case with text_to_sql DISABLED returns MODE_SKIPPED before touching
    the LLM — a keyless project must still persist that (failing) report."""
    from core.eval.golden import GoldenSet
    from core.eval.runner import models_needed

    def _set(*modes: str) -> GoldenSet:
        return GoldenSet(cases=tuple(_case(m, {"must_contain_entities": ["Acme"]}) for m in modes))

    sql_on = SimpleNamespace(text_to_sql=SimpleNamespace(enabled=True))
    sql_off = SimpleNamespace(text_to_sql=SimpleNamespace(enabled=False))
    assert models_needed(_set("graph", "global"), cast(Any, sql_on)) == (False, False)
    assert models_needed(_set("sql"), cast(Any, sql_on)) == (False, True)
    assert models_needed(_set("sql"), cast(Any, sql_off)) == (False, False)  # skipped pre-LLM
    assert models_needed(_set("semantic"), cast(Any, sql_off)) == (True, False)
    assert models_needed(_set("hybrid"), cast(Any, sql_off)) == (True, True)  # selector LLM


async def test_run_eval_refuses_when_the_persist_hits_no_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind-time check ≠ invariant: a concurrent prune can delete the build
    between binding and the persist UPDATE — a report the gate can never
    read must not print as success (rowcount 0 → LookupError, nothing
    persisted)."""
    import core.eval.runner as runner_module
    from core.eval.golden import GoldenSet
    from core.eval.runner import run_eval

    binding = SimpleNamespace(project="p", build_id=uuid.uuid4())
    monkeypatch.setattr(
        runner_module, "resolve_eval_binding", lambda conn, project, build_id: _async(binding)
    )
    for cls_name in (
        "BuildScopedRepo",
        "BuildScopedVectorRepo",
        "BuildScopedSqlReader",
        "BuildScopedGraphRepo",
    ):
        monkeypatch.setattr(
            runner_module, cls_name, SimpleNamespace(bound_to=lambda *a, **k: SimpleNamespace())
        )

    async def _global(*args: Any, **kwargs: Any) -> McpResponse:
        return McpResponse(
            query="q",
            tool="global_summary",
            project="p",
            build_id="b",
            results=(),
            warnings=(),
        )

    monkeypatch.setattr(runner_module, "global_summary", _global)

    class _Txn:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Conn:
        async def rollback(self) -> None:
            return None

        def begin(self) -> _Txn:
            return _Txn()

        async def execute(self, statement: Any) -> Any:
            return SimpleNamespace(rowcount=0)  # the build vanished

    policy = SimpleNamespace(top_k=lambda requested: 5)
    golden = GoldenSet(cases=(_case("global", {"must_contain_entities": ["Acme"]}),))
    with pytest.raises(LookupError, match="disappeared before the eval report"):
        await run_eval(
            cast(Any, _Conn()),
            cast(Any, None),
            cast(Any, None),
            None,
            None,
            "p",
            binding.build_id,
            golden,
            cast(Any, policy),
        )


async def test_expected_edges_synthesizes_cited_typed_relations() -> None:
    """Codex round 11: untyped shortest_path + entity-first subgraph budget
    can both miss the expected typed edge on a dense, valid build. The
    runner asks the SoR directly for each expected (src, dst, type) and
    synthesizes a rendered, evidence-cited relation result — and nothing
    when the edge does not exist (retrieval widens, truth does not)."""
    from core.eval.runner import _expected_edges

    src_id, dst_id, rel_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    class _Repo:
        project = "p"
        build_id = "b"

        async def entity_ids_by_name(self, name: str) -> list[uuid.UUID]:
            return {"Acme": [src_id], "Globex": [dst_id]}.get(name, [])

        async def relations_with_evidence(self, triples: Any) -> Any:
            wanted = (src_id, dst_id, "partners_with")
            if wanted in triples:
                return {
                    wanted: (
                        rel_id,
                        [
                            {
                                "evidence_type": "manual",
                                "evidence_ref": "s3://a.txt",
                                "source_uri": "s3://a.txt",
                                "quote": "Acme partners with Globex",
                            }
                        ],
                    )
                }
            return {}

    class _Graph:
        def __init__(self, edges: list[dict[str, str]]) -> None:
            self._edges = edges

        async def has_edge(self, src: str, dst: str, rel_type: str, *, timeout_ms: int) -> bool:
            return any(
                e["src"] == src and e["dst"] == dst and e["type"] == rel_type for e in self._edges
            )

    projected = _Graph([{"src": str(src_id), "dst": str(dst_id), "type": "partners_with"}])
    policy = SimpleNamespace(cypher_policy=lambda: SimpleNamespace(max_rows=100, timeout_ms=5000))
    case = _case(
        "graph",
        {"must_include_relations": [{"src": "Acme", "type": "partners_with", "dst": "Globex"}]},
    )
    response = await _expected_edges(
        cast(Any, _Repo()), cast(Any, projected), cast(Any, policy), case
    )
    assert response is not None
    assert response.results[0].title == "Acme -[partners_with]-> Globex"
    assert response.results[0].source_refs  # §27.2: evidence-cited
    assert response.results[0].id == str(rel_id)

    missing = _case(
        "graph",
        {"must_include_relations": [{"src": "Acme", "type": "owns", "dst": "Globex"}]},
    )
    assert (
        await _expected_edges(cast(Any, _Repo()), cast(Any, projected), cast(Any, policy), missing)
        is None
    )

    # the SoR alone is NOT enough (Codex round 14): a projection holding a
    # different/stale edge — §19 count-balanced — must not pass on Postgres
    stale = _Graph([{"src": str(src_id), "dst": str(dst_id), "type": "owns"}])
    assert (
        await _expected_edges(cast(Any, _Repo()), cast(Any, stale), cast(Any, policy), case) is None
    )
