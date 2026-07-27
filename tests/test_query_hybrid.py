"""Why: hybrid_query fans one question across the three fused modes
(semantic/graph/sql) DETERMINISTICALLY (MCP8: the LLM selector is gone —
every available mode runs, always; global is never fused and the skip is
said). What must hold: policy/parameter gating is surfaced, one mode's crash
degrades to the remaining modes (§22 verbatim), fusion is deterministic
rank-based merging with the origin mode's raw score preserved in confidence,
the trace tells the truth about what ran, and the debug block obeys
expose_debug. Every response is validated against the frozen §16 schema —
including the debug shape.
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
from core.query.graph import GraphQueryParams
from core.query.hybrid import HybridDeps, HybridPolicy, _fuse, hybrid_query
from core.query.mentions import mention_warnings
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
        llm=cast(Any, llm or _FakeLLM()),
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
) -> dict[str, list[Any]]:
    """Replace the three fused mode functions; record calls. A value of None
    installs an empty-result stub; an Exception instance installs a raiser.
    (global is NOT fused since MCP8 — hybrid never calls it.)"""
    calls: dict[str, list[Any]] = {"semantic": [], "graph": [], "sql": []}

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


async def test_every_available_mode_runs_and_no_llm_is_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP8 REVERSED the v1 LLM-selector pins that stood here (narrowing,
    eight broken-answer fallbacks, raising-selector fallback): selection is
    now DETERMINISTIC — every available mode runs, the LLM is never asked
    (measured: the selector cost 1,525ms, half the hybrid latency, and its
    under-selection was the top quality defect; its any-failure fallback was
    the better behavior all along). The trace says so, and global is always
    listed skipped (not fused — MCP3's not-query-matched rule)."""
    calls = _patch_modes(monkeypatch)
    llm = _FakeLLM(json.dumps({"modes": ["semantic"], "reason": "should never be read"}))
    response = await _run(_deps(llm), _policy())
    assert llm.calls == 0  # the selector is GONE, not just ignored
    assert all(len(calls[mode]) == 1 for mode in ("semantic", "graph", "sql"))
    assert response.debug is not None
    decision = response.debug["routing_decision"]
    assert decision["selected"] == ["semantic", "graph", "sql"]
    assert "global" in decision["skipped"]
    assert "deterministic fan-out" in decision["reason"]


async def test_gated_modes_are_surfaced_with_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy/parameter gating: a disabled sql mode and a graph mode without
    params are skipped with reasons (MODE_SKIPPED) — and since MCP8 global
    is ALWAYS in the skipped set (not fused; the reason points the agent at
    global_summary for corpus overview)."""
    calls = _patch_modes(monkeypatch)
    response = await _run(_deps(), _policy(sql_enabled=False), graph_params=None)
    assert calls["sql"] == [] and calls["graph"] == []
    skipped_warnings = [w.message for w in response.warnings if w.code == "MODE_SKIPPED"]
    assert any("sql mode skipped" in m for m in skipped_warnings)
    assert any("graph mode skipped" in m for m in skipped_warnings)
    assert any("global_summary" in m for m in skipped_warnings)  # the real path
    assert response.debug is not None
    assert sorted(response.debug["routing_decision"]["skipped"]) == ["global", "graph", "sql"]


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
        sql=_mode_response("sql_query", keeper),
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


async def test_global_is_never_fused_and_the_skip_names_the_real_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP8 REVERSED the MCP3-era fusion-filter pins that stood here (the
    [global] LOW_CONFIDENCE and refs-cap warnings dying with their clipped
    reports): global is no longer fused AT ALL — community reports are
    rating-ranked corpus overview, never query-matched (MCP3's own finding),
    and fusing them spent 3-5 page slots on results irrelevant to the
    question. Every call SAYS so (MODE_SKIPPED naming global_summary — a
    real path, #124), so the exclusion is never silent."""
    calls = _patch_modes(
        monkeypatch, semantic=_mode_response("semantic_search", _result(rid="hit"))
    )
    response = await _run(_deps(), _policy())
    assert "global" not in calls  # the harness has no global seam left to call
    assert all(r.result_type != "community_report" for r in response.results)
    skip = [w for w in response.warnings if w.code == "MODE_SKIPPED" and "global" in w.message]
    assert len(skip) == 1 and "global_summary" in skip[0].message
    assert not any(w.code == "LOW_CONFIDENCE" for w in response.warnings)


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


def test_fusion_keeps_a_floor_of_passages_so_the_page_is_never_all_entities() -> None:
    """The documented DEFAULT tool must not answer with a page carrying no text.

    QA2/D1: RRF ranks every mode against every other, so a graph mode returning
    many strong entities took the WHOLE page — a real visitor question came back
    20/20 `result_type=entity` with every `text` empty, while single-mode
    `semantic_search` on the same question returned 10 answer-bearing chunks.
    The flagship was strictly worse than the tool it fuses, and said nothing.

    Same skew `semantic._fair_page` already fixes one facade over (MCP6), so the
    floor takes that helper's shape — including its §22 over-block dual, which
    is the second half of this test: a SCARCE passage set must never cost the
    other bucket its slots, or the fix for an empty page becomes a starved one.
    """
    # Faithful to the live repro, which the first draft of this fixture was
    # NOT (its probe stayed green): RRF scores by RANK WITHIN a mode's list, so
    # what kills the chunks is their POSITION in semantic's own output. semantic
    # fair-pages 10 entities + 10 chunks, then §16 ordering sorts by score and
    # the entity cosines outrank the chunk ones — so semantic's chunks sit at
    # ranks 11-20 (RRF 1/71..1/80) while BOTH modes' entities hold ranks 1-10
    # (1/61..1/70). Twenty entities therefore outscore every chunk outright.
    sem_entities = tuple(_result("entity", rid=f"se{i}", score=0.7 - i * 0.01) for i in range(10))
    sem_chunks = tuple(_result("chunk", rid=f"c{i}", score=0.5 - i * 0.01) for i in range(10))
    semantic = sem_entities + sem_chunks  # entities FIRST — the real ordering
    entities = tuple(_result("entity", rid=f"ge{i}", score=1.0 - i * 0.01) for i in range(20))

    fused, truncated = _fuse([entities, semantic], top_k=20)
    kinds = [r.result_type for r in fused]
    assert len(fused) == 20 and truncated is True
    # the defect: this was 0 before the floor. Floors are 4:2:1:1 — passages
    # keep half the page, graph facts a quarter, sql rows and names an eighth
    # each — so with no facts or rows in play their unused slots flow to names
    # on rank, and passages still hold their measured half.
    assert kinds.count("chunk") == 10, "passages must hold their half of the page"
    assert kinds.count("entity") == 10

    # over-block dual: one lone chunk takes ONE slot, never a reserved ten
    lone, _ = _fuse([entities, sem_entities + (sem_chunks[0],)], top_k=20)
    lone_kinds = [r.result_type for r in lone]
    assert len(lone) == 20
    assert lone_kinds.count("chunk") == 1 and lone_kinds.count("entity") == 19

    # and a page with no passages at all is still a full page, not a short one
    none_left, _ = _fuse([entities], top_k=20)
    assert len(none_left) == 20 and all(r.result_type == "entity" for r in none_left)

    # The floor is a strict NO-OP whenever nothing is being clipped, which is
    # why it needs no warning of its own: it can only ever change WHICH results
    # a page keeps, and that case already carries TRUNCATED. If it could evict
    # silently, an agent would lose a result with nothing said about it (§22).
    small = entities[:6] + sem_chunks[:2]
    fits, fits_truncated = _fuse([small], top_k=20)
    assert fits_truncated is False
    assert {r.id for r in fits} == {r.id for r in small}  # nothing evicted at all

    # Each share ROUNDS DOWN (floors are top_k//2, //4, //8, //8), so a page
    # too small to divide reserves nothing and rank alone decides — the single
    # slot can legitimately be an entity even when a chunk was available (Codex
    # #142 r1). Pinned because the tool description states this edge: promising
    # otherwise would either lie, or force a passage over the caller's own
    # ranking on a page they deliberately narrowed to one.
    both_modes = (_result("entity", rid="shared", score=1.0),)
    one_each = (_result("entity", rid="shared", score=0.7), _result("chunk", rid="ck", score=0.5))
    single, _ = _fuse([both_modes, one_each], top_k=1)
    assert [r.result_type for r in single] == ["entity"]  # the doubly-ranked entity wins
    # ...and the passage half appears from top_k=2, which is where the
    # description says it does — the only pin on where the guarantee BEGINS
    pair, _ = _fuse([both_modes, one_each], top_k=2)
    assert [r.result_type for r in pair].count("chunk") == 1

    # STATED FACTS ARE NOT NAMES, AND SQL ROWS ARE NOT GRAPH FACTS (Codex #142
    # r2 + gate-2). `core.query.graph._score` positions relations AFTER
    # entities in the graph mode's own list, so a bucket shared with entities
    # spent every slot on entities and deleted the graph answers this change
    # never aimed at — depressing §20 relation_hit_rate with them. Rows then
    # reproduced it one level down: sql rows hold ranks 1..N of their OWN list,
    # so sharing a bucket with the demoted relations let rows take the whole
    # share and evict every relation. Separate buckets keep both.
    graph_dense = tuple(
        _result("entity", rid=f"gd{i}", score=1.0 - i * 0.01) for i in range(2)
    ) + tuple(_result("relation", rid=f"rel{i}", score=0.5 - i * 0.01) for i in range(12))
    sem_mixed = tuple(
        _result("entity", rid=f"sm{i}", score=0.7 - i * 0.01) for i in range(10)
    ) + tuple(_result("chunk", rid=f"cm{i}", score=0.4 - i * 0.01) for i in range(8))
    facts = [r.result_type for r in _fuse([graph_dense, sem_mixed], top_k=20)[0]]
    assert facts.count("relation") == 5, "graph facts must not compete with names"
    assert facts.count("chunk") == 8  # and not at the passage share's expense

    sql_rows = tuple(_result("row", rid=f"rw{i}", score=0.9 - i * 0.01) for i in range(10))
    mixed = [r.result_type for r in _fuse([graph_dense, sem_mixed, sql_rows], top_k=20)[0]]
    assert mixed.count("relation") == 5, "rows must not evict the demoted relations"
    assert mixed.count("row") == 3 and mixed.count("chunk") == 8


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


async def test_the_whole_call_shares_one_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§21: max_latency_ms bounds the WHOLE hybrid call — per-mode DB timeouts
    alone don't, because modes run sequentially and auto-plan/embedding work
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
    assert all(len(calls[mode]) == 1 for mode in ("semantic", "graph", "sql"))
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
        llm=cast(Any, llm or _FakeLLM()),
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


async def test_auto_planned_graph_runs_at_its_mode_order_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The golden cases need relation-path questions to RUN graph mode. With
    the LLM selector removed (MCP8) the guarantee is structural — an
    auto-planned graph mode is simply available and therefore runs, at its
    _MODE_ORDER position (modes run sequentially against one shared
    deadline; a last-place graph would be the first cut on a tight budget)."""
    calls = _patch_modes(monkeypatch)
    deps, _repo = _linkable_deps(["區域探索廳"])
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
    assert routing["selected"] == ["semantic", "graph", "sql"]  # _MODE_ORDER position


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
        sql=_mode_response("sql_query", keeper),
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
        sql=_mode_response("sql_query", keeper),
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
            sql=_mode_response("sql_query", keeper),
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
        sql=_mode_response("sql_query", keeper),
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


async def test_mention_loss_warnings_are_refit_to_the_fused_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #127 r4 (the MCP3 provenance arc, applied to mention warnings):
    a mode computes its cap/drop warnings against its OWN page, but fusion
    may clip the affected entity — the fused response must not claim a
    returned entity lost citations when none of the named entities survived.
    The messages name their entities, so hybrid REBUILDS each warning for
    the fused page via the builder/parser siblings."""
    # fixed high uuid: sorts AFTER "a-hit" so the id tie-break clips IT
    capped_id = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    warnings = mention_warnings({capped_id}, {capped_id: 2}, {capped_id})

    def _entity(rid: str) -> RetrievalResult:
        # v1.1 entity minimum: a RESOLVED chunk mention ref
        return _result(
            result_type="entity",
            rid=rid,
            source_refs=(
                SourceRef(
                    source_type="chunk",
                    id=str(uuid.uuid4()),
                    source_uri="s3://m.md",
                    metadata={"quote": "q", "start_offset": 0, "end_offset": 1},
                ),
            ),
        )

    _patch_modes(
        monkeypatch,
        semantic=_mode_response(
            "semantic_search", _entity(str(capped_id)), warnings=tuple(warnings)
        ),
        graph=_mode_response("graph_query", _result(rid="a-hit")),
    )
    # the affected entity SURVIVES fusion → both warnings survive, prefixed
    kept = await _run(_deps(), _policy(top_k=10))
    assert any(str(capped_id) in w.message and "capped" in w.message for w in kept.warnings)
    assert any(str(capped_id) in w.message and "unresolvable" in w.message for w in kept.warnings)

    # the affected entity is CLIPPED (top_k=1, "a-hit" wins the id tie) →
    # both mention-loss warnings die with it
    clipped = await _run(_deps(), _policy(top_k=1))
    assert [r.id for r in clipped.results] == ["a-hit"]
    assert not any("mention" in w.message for w in clipped.warnings)


async def test_fused_results_carry_the_origin_modes_raw_score_as_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP8: RRF flattens every score to ~1/61 (measured 0.0164 vs the real
    cosines 0.7224/0.5025) — the agent's only confidence signal died in
    fusion. The origin mode's RAW score now rides in `confidence` (first
    mode's on duplicate merge, same winner as the payload; clamped to the
    schema's 0..1), while `score` stays the rank-fusion ordering value."""
    shared = "shared-id"
    _patch_modes(
        monkeypatch,
        semantic=_mode_response(
            "semantic_search",
            _result(rid=shared, score=0.7224),
            _result(rid="only-semantic", score=0.5025),
        ),
        sql=_mode_response("sql_query", _result(rid=shared, score=1.0)),
    )
    response = await _run(_deps(), _policy())
    by_id = {r.id: r for r in response.results}
    assert by_id["only-semantic"].confidence == 0.5025  # the real cosine survives
    assert by_id[shared].confidence == 0.7224  # duplicate merge: FIRST mode's raw score
    # ordering is still rank-fusion — the duplicate (two rank contributions)
    # outranks the single-mode hit despite its lower origin score
    assert response.results[0].id == shared
    assert response.results[0].score != 0.7224  # score stays the RRF value
