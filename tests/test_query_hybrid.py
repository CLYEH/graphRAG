"""Why: hybrid_query is §9's default entry — one question fanned across four
modes, fused, traced. What must hold: the selector's untrusted answer can
gate NOTHING silently (any failure → every available mode runs, §22 breadth
over silence), policy/parameter gating happens before selection, one mode's
crash degrades to the remaining modes (§22 verbatim), fusion is deterministic
rank-based merging (mode scores are incomparable), the trace tells the truth
about what ran, and the debug block obeys expose_debug. Every response is
validated against the frozen §16 schema — including the debug shape.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
import pytest

import core.query.hybrid as hybrid_module
from core.query.global_reports import refs_cap_warning
from core.query.graph import GraphQueryParams
from core.query.hybrid import HybridDeps, HybridPolicy, _fuse, hybrid_query
from core.query.policy import (
    CYPHER_ALLOWED_CLAUSES,
    CYPHER_BLOCKED_MIN,
    SQL_BLOCKED_KEYWORDS_MIN,
    TextToCypher,
    TextToSql,
)
from core.query.results import McpResponse, QueryWarning, RetrievalResult, SourceRef

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)

_PROJECT = "acme"
_BUILD = uuid.UUID("7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d")


class _Scoped:
    def __init__(self, project: str = _PROJECT, build_id: uuid.UUID = _BUILD) -> None:
        self.project = project
        self.build_id = build_id


class _FakeLLM:
    def __init__(self, answer: str | None = None, raise_exc: Exception | None = None) -> None:
        self._answer = answer
        self._raise = raise_exc
        self.calls = 0

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return SimpleNamespace(message=SimpleNamespace(content=self._answer))


def _deps(llm: _FakeLLM | None = None, graph_build: uuid.UUID = _BUILD) -> HybridDeps:
    return HybridDeps(
        repo=cast(Any, _Scoped()),
        vectors=cast(Any, _Scoped()),
        embedder=cast(Any, object()),
        sql_reader=cast(Any, _Scoped()),
        graph=cast(Any, _Scoped(build_id=graph_build)),
        llm=cast(Any, llm or _FakeLLM(_pick_all())),
    )


def _policy(
    *,
    sql_enabled: bool = True,
    top_k: int = 10,
    expose_debug: bool = True,
    max_latency_ms: int = 30_000,
) -> HybridPolicy:
    return HybridPolicy(
        text_to_sql=TextToSql(
            enabled=sql_enabled,
            allowed_tables=("orders",) if sql_enabled else (),
            blocked_keywords=SQL_BLOCKED_KEYWORDS_MIN,
            max_rows=50,
            timeout_ms=1000,
        ),
        text_to_cypher=TextToCypher(
            enabled=False,
            allowed_clauses=CYPHER_ALLOWED_CLAUSES,
            blocked=CYPHER_BLOCKED_MIN,
            max_rows=50,
            timeout_ms=1000,
        ),
        max_graph_hops=3,
        top_k=top_k,
        max_sql_rows=50,
        expose_debug=expose_debug,
        max_latency_ms=max_latency_ms,
    )


def _pick_all() -> str:
    return json.dumps({"modes": ["semantic", "graph", "sql", "global"], "reason": "run everything"})


def _result(result_type: str = "chunk", rid: str | None = None, **kwargs: Any) -> RetrievalResult:
    return RetrievalResult(
        result_type=result_type,
        id=rid or str(uuid.uuid4()),
        score=kwargs.pop("score", 1.0),
        source_refs=kwargs.pop(
            "source_refs",
            (
                SourceRef(
                    source_type="chunk",
                    id=str(uuid.uuid4()),
                    source_uri="file:///x",
                    metadata={"start_offset": 0, "end_offset": 5},
                ),
            ),
        ),
        **kwargs,
    )


def _mode_response(
    tool: str, *results: RetrievalResult, warnings: tuple[QueryWarning, ...] = ()
) -> McpResponse:
    return McpResponse(
        query="q",
        tool=tool,
        project=_PROJECT,
        build_id=str(_BUILD),
        results=tuple(results),
        warnings=warnings,
    )


def _patch_modes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    semantic: Any = None,
    graph: Any = None,
    sql: Any = None,
    global_: Any = None,
) -> dict[str, list[Any]]:
    """Replace the four mode functions; record calls. A value of None installs
    an empty-result stub; an Exception instance installs a raiser."""
    calls: dict[str, list[Any]] = {"semantic": [], "graph": [], "sql": [], "global": []}

    def _install(name: str, target: str, canned: Any, maker: Any) -> None:
        async def stub(*args: Any, **kwargs: Any) -> McpResponse:
            calls[name].append(args)
            if isinstance(canned, Exception):
                raise canned
            return cast(McpResponse, canned) if canned is not None else cast(McpResponse, maker())

        monkeypatch.setattr(hybrid_module, target, stub)

    _install("semantic", "semantic_search", semantic, lambda: _mode_response("semantic_search"))
    _install("graph", "graph_query", graph, lambda: _mode_response("graph_query"))
    _install("sql", "sql_query", sql, lambda: _mode_response("sql_query"))
    _install("global", "global_summary", global_, lambda: _mode_response("global_summary"))
    return calls


_GRAPH_PARAMS = GraphQueryParams(template="neighbors", entity="Acme", hops=2)


async def _run(
    deps: HybridDeps,
    policy: HybridPolicy,
    graph_params: GraphQueryParams | None = _GRAPH_PARAMS,
) -> McpResponse:
    response = await hybrid_query(deps, policy, "the question", graph_params)
    _VALIDATOR.validate(response.to_dict())
    return response


def _codes(response: McpResponse) -> list[str]:
    return [w.code for w in response.warnings]


async def test_mismatched_scopes_fail_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fusion mixes every store's output — a split scope would cross builds
    (DR-006), so the mismatch is a bug, not a warning."""
    _patch_modes(monkeypatch)
    with pytest.raises(ValueError, match="different scopes"):
        await hybrid_query(_deps(graph_build=uuid.uuid4()), _policy(), "q", _GRAPH_PARAMS)


@pytest.mark.parametrize("bad", [0, True, "3"])
async def test_an_out_of_contract_top_k_degrades_typed(
    monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    _patch_modes(monkeypatch)
    response = await _run(_deps(), _policy(top_k=bad))
    assert response.results == () and _codes(response) == ["GUARDRAIL_BLOCKED"]


async def test_the_selector_narrows_and_the_trace_tells_the_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid selection runs ONLY those modes; the routing trace reports
    selected vs skipped (gated + unselected) with the selector's reason."""
    calls = _patch_modes(monkeypatch)
    llm = _FakeLLM(json.dumps({"modes": ["semantic", "global"], "reason": "topical question"}))
    response = await _run(_deps(llm), _policy())
    assert len(calls["semantic"]) == 1 and len(calls["global"]) == 1
    assert calls["sql"] == [] and calls["graph"] == []
    assert response.debug is not None
    decision = response.debug["routing_decision"]
    assert decision["selected"] == ["semantic", "global"]
    assert sorted(decision["skipped"]) == ["graph", "sql"]
    assert decision["reason"] == "topical question"


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps(["a", "list"]),
        json.dumps({"reason": "no modes field"}),
        json.dumps({"modes": "semantic", "reason": "wrong type"}),
        json.dumps({"modes": [1, 2], "reason": "wrong item types"}),
        json.dumps({"modes": ["teleport"], "reason": "out of vocabulary"}),
        json.dumps({"modes": ["semantic", "teleport"], "reason": "MIXED valid + hallucinated"}),
        json.dumps({"modes": [], "reason": "empty"}),
    ],
)
async def test_a_broken_selector_runs_every_available_mode(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """The selector's answer is UNTRUSTED (C3b): any failure — parse, shape,
    out-of-vocabulary, empty — must widen to every available mode, never
    silently drop one (§22: breadth over silence)."""
    calls = _patch_modes(monkeypatch)
    response = await _run(_deps(_FakeLLM(answer)), _policy())
    assert all(len(calls[mode]) == 1 for mode in ("semantic", "graph", "sql", "global"))
    assert response.debug is not None
    assert sorted(response.debug["routing_decision"]["selected"]) == [
        "global",
        "graph",
        "semantic",
        "sql",
    ]


async def test_a_raising_selector_also_runs_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_modes(monkeypatch)
    response = await _run(_deps(_FakeLLM(raise_exc=RuntimeError("llm down"))), _policy())
    assert all(len(calls[mode]) == 1 for mode in ("semantic", "graph", "sql", "global"))
    assert response.debug is not None
    assert "selector failed" in response.debug["routing_decision"]["reason"]


async def test_gated_modes_never_reach_the_selector_and_are_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy/parameter gating happens BEFORE selection: a disabled sql mode
    and a graph mode without params are skipped with reasons (MODE_SKIPPED)
    and the selector cannot pick them."""
    calls = _patch_modes(monkeypatch)
    llm = _FakeLLM(_pick_all())
    response = await _run(_deps(llm), _policy(sql_enabled=False), graph_params=None)
    assert calls["sql"] == [] and calls["graph"] == []
    skipped_warnings = [w.message for w in response.warnings if w.code == "MODE_SKIPPED"]
    assert any("sql mode skipped" in m for m in skipped_warnings)
    assert any("graph mode skipped" in m for m in skipped_warnings)
    assert response.debug is not None
    assert sorted(response.debug["routing_decision"]["skipped"]) == ["graph", "sql"]


async def test_a_crashing_mode_degrades_to_the_remaining_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§22 verbatim: one store down ≠ hybrid down — the crashing mode yields
    a typed STORE_UNAVAILABLE naming it, the others' results still fuse, and
    the debug plan reports only what actually ran."""
    keeper = _result(rid="kept")
    _patch_modes(
        monkeypatch,
        semantic=RuntimeError("qdrant refused"),
        global_=_mode_response("global_summary", keeper),
    )
    response = await _run(_deps(), _policy())
    assert [r.id for r in response.results] == ["kept"]
    unavailable = [w for w in response.warnings if w.code == "STORE_UNAVAILABLE"]
    assert len(unavailable) == 1 and "semantic mode failed" in unavailable[0].message
    assert response.debug is not None
    assert not any(plan.startswith("semantic") for plan in response.debug["retrieval_plan"])


async def test_mode_warnings_are_aggregated_with_their_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mode's internal degradations survive fusion — code preserved (the
    frozen §22 enum), message prefixed with the mode so the operator can tell
    whose truncation it was."""
    _patch_modes(
        monkeypatch,
        sql=_mode_response(
            "sql_query", warnings=(QueryWarning("TRUNCATED", "result truncated (§21)"),)
        ),
    )
    response = await _run(_deps(), _policy())
    truncs = [w for w in response.warnings if w.code == "TRUNCATED"]
    assert any(w.message.startswith("[sql]") for w in truncs)


async def test_globals_low_confidence_dies_with_its_reports_in_fusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #123 r2: the global mode's LOW_CONFIDENCE qualifies COMMUNITY
    REPORTS (rating-ranked, never query-matched). When fusion clips every
    report off the page, propagating it anyway tells the agent the surviving,
    query-matched results are unreliable — the warning must live and die with
    its subjects."""
    low_conf = QueryWarning("LOW_CONFIDENCE", "global results are ranked by community rating …")
    _patch_modes(
        monkeypatch,
        semantic=_mode_response("semantic_search", _result(rid="a-hit")),
        global_=_mode_response(
            "global_summary",
            _result(
                result_type="community_report",
                rid="z-report",
                # §27.2/§16: a community_report result must cite entity refs
                source_refs=(SourceRef(source_type="entity", id=str(uuid.uuid4())),),
            ),
            warnings=(low_conf,),
        ),
    )
    # equal RRF (both rank 1) → id ASC tie-break: "a-hit" wins, the report is
    # clipped by top_k=1 — no community_report on the page, no warning
    clipped = await _run(_deps(), _policy(top_k=1))
    assert [r.id for r in clipped.results] == ["a-hit"]
    assert not any(w.code == "LOW_CONFIDENCE" for w in clipped.warnings)

    # when the report DOES survive fusion, the warning survives with it
    kept = await _run(_deps(), _policy(top_k=10))
    assert any(r.result_type == "community_report" for r in kept.results)
    low = [w for w in kept.warnings if w.code == "LOW_CONFIDENCE"]
    assert len(low) == 1 and low[0].message.startswith("[global]")


async def test_globals_refs_cap_warning_dies_when_the_named_report_is_clipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #123 r4: the cap warning must track PROVENANCE, not a length
    proxy — a COMPLETE report with exactly REFS_CAP refs surviving fusion
    while the actually-capped report is clipped would keep a false "refs
    omitted" claim under any len(source_refs)-based rule. The warning names
    its report; it lives exactly as long as that report is on the fused
    page."""
    cap_warning = refs_cap_warning("z-capped", 12)

    def _entity_refs(count: int) -> tuple[SourceRef, ...]:
        return tuple(SourceRef(source_type="entity", id=str(uuid.uuid4())) for _ in range(count))

    _patch_modes(
        monkeypatch,
        semantic=_mode_response("semantic_search", _result(rid="a-hit")),
        global_=_mode_response(
            "global_summary",
            # b-full is COMPLETE at exactly REFS_CAP members — the length-proxy trap
            _result(result_type="community_report", rid="b-full", source_refs=_entity_refs(8)),
            _result(result_type="community_report", rid="z-capped", source_refs=_entity_refs(8)),
            warnings=(cap_warning,),
        ),
    )
    # top_k=2 keeps "a-hit" + "b-full" (rank-1 tie → id ASC; z-capped at
    # rank 2 is clipped): the warning's named report is gone, so the warning
    # goes too — even though an at-cap-length report survived
    clipped = await _run(_deps(), _policy(top_k=2))
    assert [r.id for r in clipped.results] == ["a-hit", "b-full"]
    assert not any("source_refs capped" in w.message for w in clipped.warnings)

    # the named report on the page is what makes the warning true — kept
    kept = await _run(_deps(), _policy(top_k=10))
    assert any(r.id == "z-capped" for r in kept.results)
    capped = [w for w in kept.warnings if "source_refs capped" in w.message]
    assert len(capped) == 1 and capped[0].message.startswith("[global]")
    assert "z-capped" in capped[0].message


async def test_fusion_merges_duplicates_and_ranks_by_rrf() -> None:
    """RRF: mode scores are incomparable, ranks are the shared currency. A
    result found by TWO modes accumulates both rank contributions (so it
    outranks single-mode hits at the same ranks) and its refs union without
    duplicates; the first mode's payload wins deterministically."""
    shared_id = "shared"
    ref_a = SourceRef(
        source_type="chunk",
        id="c1",
        source_uri="file:///a",
        metadata={"start_offset": 0, "end_offset": 5},
    )
    ref_b = SourceRef(source_type="row", id="r1", metadata={"table": "t", "pk": "1"})
    from_semantic = _result(rid=shared_id, source_refs=(ref_a,), title="semantic view")
    from_sql = _result(rid=shared_id, source_refs=(ref_a, ref_b), title="sql view")
    solo = _result(rid="solo")
    fused, truncated = _fuse([(from_semantic, solo), (from_sql,)], top_k=10)
    assert truncated is False
    assert [r.id for r in fused] == [shared_id, "solo"]  # 1/61+1/61 > 1/62
    merged = fused[0]
    assert merged.title == "semantic view"  # first mode's payload wins
    assert [(ref.source_type, ref.id) for ref in merged.source_refs] == [
        ("chunk", "c1"),
        ("row", "r1"),
    ]  # union, no duplicate
    assert abs(merged.score - 2 / 61) < 1e-12
    assert abs(fused[1].score - 1 / 62) < 1e-12


async def test_fusion_clips_to_top_k_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [_result(rid=f"r{i}") for i in range(3)]
    _patch_modes(monkeypatch, semantic=_mode_response("semantic_search", *results))
    response = await _run(_deps(), _policy(top_k=2))
    assert len(response.results) == 2
    assert "TRUNCATED" in _codes(response)


async def test_debug_is_null_when_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    """§16/§21: the debug block exists ONLY when expose_debug allows it —
    otherwise null, not an empty object."""
    _patch_modes(monkeypatch)
    response = await _run(_deps(), _policy(expose_debug=False))
    assert response.debug is None
    assert response.to_dict()["debug"] is None


async def test_a_single_available_mode_skips_the_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One available mode = nothing to select — the LLM is not consulted
    (latency and an untrusted surface avoided for free)."""
    calls = _patch_modes(monkeypatch)
    llm = _FakeLLM(_pick_all())
    monkeypatch.setattr(hybrid_module, "_MODE_ORDER", ("semantic",))
    await _run(_deps(llm), _policy())
    assert llm.calls == 0  # selector never consulted
    assert len(calls["semantic"]) == 1


async def test_a_mode_outside_the_offered_set_distrusts_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selector is TOLD the available set; naming a real-but-GATED mode is
    the same non-compliance as a hallucinated one — the whole answer is
    distrusted and every available mode runs (silently honoring the valid
    half would narrow retrieval without a warning)."""
    calls = _patch_modes(monkeypatch)
    llm = _FakeLLM(json.dumps({"modes": ["semantic", "sql"], "reason": "sql is gated"}))
    response = await _run(_deps(llm), _policy(sql_enabled=False))  # sql gated by policy
    assert calls["sql"] == []  # the gate still holds absolutely
    assert len(calls["semantic"]) == 1 and len(calls["graph"]) == 1 and len(calls["global"]) == 1
    assert response.debug is not None
    assert "unavailable mode" in response.debug["routing_decision"]["reason"]


async def test_the_whole_call_shares_one_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21: max_latency_ms bounds the WHOLE hybrid call — per-mode DB timeouts
    alone don't, because modes run sequentially and selector/embedding work
    has no DB deadline. A mode that overruns the remaining budget is cut
    (typed PARTIAL_RESULTS naming the deadline), later modes past the budget
    never start, and the trace reports only what ran."""
    calls = _patch_modes(monkeypatch)

    async def slow_semantic(*args: Any, **kwargs: Any) -> McpResponse:
        calls["semantic"].append(args)
        await asyncio.sleep(0.2)  # far past the 50ms budget below
        return _mode_response("semantic_search")

    monkeypatch.setattr(hybrid_module, "semantic_search", slow_semantic)
    response = await _run(_deps(), _policy(max_latency_ms=50))
    partials = [w for w in response.warnings if w.code == "PARTIAL_RESULTS"]
    assert any("deadline" in w.message and "semantic" in w.message for w in partials)
    assert response.debug is not None
    # the overrunning mode never completed — the plan reports only what ran
    assert not any(plan.startswith("semantic") for plan in response.debug["retrieval_plan"])


async def test_a_generous_deadline_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_modes(monkeypatch)
    response = await _run(_deps(), _policy(max_latency_ms=30_000))
    assert all(len(calls[mode]) == 1 for mode in ("semantic", "graph", "sql", "global"))
    assert not any("deadline" in w.message for w in response.warnings)


# ---- QP1: the auto graph plan --------------------------------------------------


class _LinkableRepo(_Scoped):
    """A repo whose build knows some entity names — the QP1 linking dictionary."""

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self._names = names
        self.name_reads = 0

    async def distinct_active_entity_names(self) -> list[str]:
        self.name_reads += 1
        return list(self._names)


def _linkable_deps(
    names: list[str], llm: _FakeLLM | None = None
) -> tuple[HybridDeps, _LinkableRepo]:
    repo = _LinkableRepo(names)
    deps = HybridDeps(
        repo=cast(Any, repo),
        vectors=cast(Any, _Scoped()),
        embedder=cast(Any, object()),
        sql_reader=cast(Any, _Scoped()),
        graph=cast(Any, _Scoped()),
        llm=cast(Any, llm or _FakeLLM(_pick_all())),
    )
    return deps, repo


async def test_auto_plan_runs_graph_for_a_bare_nl_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QP1's point: a plain-language question that names a build entity gets
    the GraphRAG core WITHOUT the caller supplying template or seed — before
    this, graph was gated forever for every NL caller (review §P0#3)."""
    calls = _patch_modes(monkeypatch)
    deps, _repo = _linkable_deps(["區域探索廳"])
    response = await hybrid_query(deps, _policy(), "區域探索廳有什麼可以看的?", None)
    _VALIDATOR.validate(response.to_dict())

    assert len(calls["graph"]) == 1
    params = calls["graph"][0][3]  # graph_query(graph, repo, policy, params, ...)
    assert params.template == "neighbors" and params.entity == "區域探索廳"
    assert not any(w.code == "MODE_SKIPPED" and "graph" in w.message for w in response.warnings)
    assert response.debug is not None
    # the plan leads the trace: entities + template + seed are auditable
    assert "auto plan" in response.debug["retrieval_plan"][0]
    assert "區域探索廳" in response.debug["retrieval_plan"][0]


async def test_auto_plan_two_entities_takes_the_path_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_modes(monkeypatch)
    deps, _repo = _linkable_deps(["海科館", "區域探索廳"])
    await hybrid_query(deps, _policy(), "從海科館怎麼走到區域探索廳?", None)

    params = calls["graph"][0][3]
    assert params.template == "path"
    assert params.entity == "海科館" and params.other_entity == "區域探索廳"
    assert params.hops == 3  # the policy ceiling (max_graph_hops), not a guess


async def test_auto_planned_graph_survives_a_selector_that_skips_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The golden cases need relation-path questions to RUN graph mode — that
    guarantee cannot hinge on an LLM selector's mood (C3b: LLM-assisted,
    never LLM-trusted). An auto-planned graph mode always runs."""
    calls = _patch_modes(monkeypatch)
    # the selector picks modes that come AFTER graph in _MODE_ORDER — the
    # discriminating case: append-last would run graph LAST, ordered insert
    # runs it before them (a same-prefix selection like ["semantic"] cannot
    # tell the two apart — the first probe of this pin was false-green)
    picky = _FakeLLM(json.dumps({"modes": ["sql", "global"], "reason": "prose only"}))
    deps, _repo = _linkable_deps(["區域探索廳"], llm=picky)
    response = await hybrid_query(deps, _policy(), "區域探索廳和誰有關?", None)

    assert len(calls["graph"]) == 1
    assert response.debug is not None
    routing = response.debug["routing_decision"]
    assert "graph" not in routing["skipped"]
    assert "auto plan" in routing["reason"]
    # the joined mode sits at its _MODE_ORDER position, NOT last: modes run
    # sequentially against one shared deadline, and a last-place graph would
    # be the first cut on a tight budget — silently defeating the guarantee
    # this test exists for (Codex #89 R1)
    assert routing["selected"] == ["graph", "sql", "global"]


async def test_no_link_keeps_graph_gated_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero linked entities = zero fabricated traversals: the old gating
    stands, and the reason says the auto plan looked and found no seed."""
    calls = _patch_modes(monkeypatch)
    deps, repo = _linkable_deps(["潮境智能海洋館"])
    response = await hybrid_query(deps, _policy(), "how do refunds work?", None)

    assert repo.name_reads == 1  # linking ran…
    assert len(calls["graph"]) == 0  # …but no plan was invented
    skipped = [w.message for w in response.warnings if w.code == "MODE_SKIPPED"]
    assert any("graph mode skipped" in m and "no build entity name linked" in m for m in skipped)


async def test_caller_params_bypass_linking_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit caller params are the caller's OWN plan — the router must not
    second-guess them, and must not spend a query on the name dictionary."""
    calls = _patch_modes(monkeypatch)
    deps, repo = _linkable_deps(["區域探索廳"])
    response = await hybrid_query(deps, _policy(), "區域探索廳?", _GRAPH_PARAMS)

    assert repo.name_reads == 0  # linking never ran
    assert calls["graph"][0][3] is _GRAPH_PARAMS  # the caller's params, verbatim
    assert response.debug is not None
    assert not any("auto plan" in line for line in response.debug["retrieval_plan"])


async def test_a_caller_input_rejection_is_not_reported_as_a_store_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP2: a provider 4xx means the INPUT was rejected — an empty query makes
    the embeddings API raise 400. The old blanket STORE_UNAVAILABLE told the
    agent "infrastructure problem, back off and retry", so it retried the
    identical malformed call forever. A 4xx (except 429) must surface as
    GUARDRAIL_BLOCKED with change-the-input guidance; a 429 and a plain crash
    stay STORE_UNAVAILABLE, because retrying THOSE later can genuinely work.
    """

    class _Rejected(RuntimeError):
        status_code = 400

    class _Throttled(RuntimeError):
        status_code = 429

    keeper = _result(rid="kept")

    # 400 → GUARDRAIL_BLOCKED, and the message says to change the input
    _patch_modes(
        monkeypatch,
        semantic=_Rejected("bad input"),
        global_=_mode_response("global_summary", keeper),
    )
    response = await _run(_deps(), _policy())
    blocked = [w for w in response.warnings if w.code == "GUARDRAIL_BLOCKED"]
    assert len(blocked) == 1 and "rejected the request input" in blocked[0].message
    assert "retrying unchanged will fail again" in blocked[0].message
    assert not any(w.code == "STORE_UNAVAILABLE" for w in response.warnings)
    assert [r.id for r in response.results] == ["kept"]  # still degrades, not fails

    # 429 is infrastructure-busy: retry CAN work, so it stays STORE_UNAVAILABLE
    _patch_modes(
        monkeypatch,
        semantic=_Throttled("rate limited"),
        global_=_mode_response("global_summary", keeper),
    )
    throttled = await _run(_deps(), _policy())
    assert any(w.code == "STORE_UNAVAILABLE" for w in throttled.warnings)
    assert not any(w.code == "GUARDRAIL_BLOCKED" for w in throttled.warnings)

    # 401/403/404 are credentials/permissions/missing-deployment — rewording
    # the query repairs none of them, and calling them caller-input would hide
    # a real outage from operators (Codex #122): they stay STORE_UNAVAILABLE
    for auth_status in (401, 403, 404):

        class _NotInput(RuntimeError):
            status_code = auth_status

        _patch_modes(
            monkeypatch,
            semantic=_NotInput("not an input problem"),
            global_=_mode_response("global_summary", keeper),
        )
        outage = await _run(_deps(), _policy())
        assert any(w.code == "STORE_UNAVAILABLE" for w in outage.warnings), auth_status
        assert not any(w.code == "GUARDRAIL_BLOCKED" for w in outage.warnings), auth_status

    # a STORE client's 400 is a projection fault, not the caller's: Qdrant
    # raises UnexpectedResponse(status=400) for vector-dimension drift, and
    # only repairing the projection helps — status alone must not classify
    # (Codex #122 r2)
    from qdrant_client.http.exceptions import ApiException

    class _QdrantBad(ApiException):
        status_code = 400

    _patch_modes(
        monkeypatch,
        semantic=_QdrantBad("dimension drift"),
        global_=_mode_response("global_summary", keeper),
    )
    store_400 = await _run(_deps(), _policy())
    assert any(w.code == "STORE_UNAVAILABLE" for w in store_400.warnings)
    assert not any(w.code == "GUARDRAIL_BLOCKED" for w in store_400.warnings)
    # ...and the store is NAMED (Codex #122 r3): hybrid is the default tool,
    # and "semantic mode failed (SomeClientException)" leaves the agent unable
    # to tell a Qdrant-only outage (route around it) from Postgres down
    # (everything is dead) — the same distinction the single-mode tools give
    outage_msgs = [w.message for w in store_400.warnings if w.code == "STORE_UNAVAILABLE"]
    assert any("qdrant unavailable" in m for m in outage_msgs), outage_msgs
