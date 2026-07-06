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


def test_graph_params_prefer_the_expected_relation_path() -> None:
    params = _derive_graph_params(
        _case(
            "graph",
            {"must_include_relations": [{"src": "Acme", "type": "t", "dst": "Globex"}]},
        ),
        max_hops=3,
    )
    assert params is not None
    assert (params.template, params.entity, params.other_entity, params.hops) == (
        "path",
        "Acme",
        "Globex",
        3,
    )


def test_graph_params_fall_back_to_neighbors_then_none() -> None:
    params = _derive_graph_params(_case("graph", {"must_contain_entities": ["Acme"]}), 3)
    assert params is not None and (params.template, params.entity) == ("neighbors", "Acme")
    assert _derive_graph_params(_case("graph", {"answer_regex": "x"}), 3) is None


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
    )
    payload = report.to_metrics_payload()
    assert payload["score"] == 0.75  # the §14 gate compares exactly this
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

        async def execute(self, statement: Any) -> None:
            self.executed.append(statement)

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
    assert len(conn.executed) == 1  # ONE persisting UPDATE to builds.metrics


def _async(value: Any) -> Any:
    async def _coro() -> Any:
        return value

    return _coro()
