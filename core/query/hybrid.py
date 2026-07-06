"""Hybrid retrieval: router + fusion + routing trace → §16 response (§8/§9/§16, C6e).

The §8 ``hybrid`` modality and §9's default entry (``hybrid_query``): route one
question across the four single-mode retrievers, fuse their results, and emit
the routing trace in the §16 ``debug`` block (gated by ``expose_debug``).

**Selection** is LLM-assisted but never LLM-trusted (the C3b rule): the
selector's answer is strictly validated against the AVAILABLE mode set, and
ANY failure — transport, parse, wrong shape, out-of-vocabulary modes, empty
selection — falls back to running EVERY available mode with the failure named
in the routing reason. Over-selection costs latency; silent under-selection
costs answers (§22 degrades breadth-first, never silence-first). Availability
is policy/parameter-gated before the selector ever sees a mode: ``sql`` needs
``text_to_sql.enabled``; ``graph`` needs caller-supplied
:class:`~core.query.graph.GraphQueryParams` (a bare NL question carries no
template/seed — the router does not invent them).

**Fusion** is reciprocal-rank (RRF, k=60): scores from different modes are not
comparable (cosine vs positional), ranks are. Duplicates (same result_type +
id across modes) merge — first mode's payload wins (mode order is fixed, so
the merge is deterministic), source_refs union. The fused list is clipped to
``top_k`` with TRUNCATED (§22).

**Failure boundary = the mode** (§22 verbatim: 單一 store 不可用 → hybrid 降級
為可用模態子集,warnings 標示,不整體失敗): each mode call is individually
guarded; a raising mode contributes zero results and a typed STORE_UNAVAILABLE
warning naming it. The modes' own internal degradations (their typed warnings)
are aggregated into the hybrid response.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms import LLM, ChatMessage, MessageRole

from core.query.global_reports import global_summary
from core.query.graph import GraphQueryParams, graph_query
from core.query.policy import TextToCypher, TextToSql
from core.query.results import (
    McpResponse,
    QueryWarning,
    RetrievalResult,
    SourceRef,
    ordered_results,
)
from core.query.semantic import semantic_search
from core.query.sql import sql_query
from core.stores.graph import BuildScopedGraphRepo
from core.stores.repo import BuildScopedRepo
from core.stores.sqlreader import BuildScopedSqlReader
from core.stores.vectors import BuildScopedVectorRepo

_TOOL = "hybrid_query"

#: Fixed mode order — selection, fusion tie-breaks, and duplicate-merge
#: precedence all derive from it, so the response is deterministic.
_MODE_ORDER = ("semantic", "graph", "sql", "global")

#: RRF's standard damping constant — rank 1 scores 1/61; the exact value only
#: shifts absolute scores, never the relative order for a single list.
_RRF_K = 60

#: §16 debug.stores_used per mode (the stores a mode reads).
_MODE_STORES = {
    "semantic": ("qdrant", "postgres"),
    "graph": ("neo4j", "postgres"),
    "sql": ("postgres",),
    "global": ("postgres",),
}

_SELECTOR_SYSTEM = """\
You route a question to retrieval modes. Reply with ONLY a JSON object shaped
exactly: {"modes": ["<mode>", ...], "reason": "<one short sentence>"}
Available modes and what they are good at:
- semantic: fuzzy/topical questions over document text
- graph: relationships between named entities (who connects to whom, paths)
- sql: precise filters/lookups over structured rows
- global: corpus-wide themes and summaries
Pick every mode that could plausibly help; prefer more over fewer.
"""


@dataclass(frozen=True)
class HybridDeps:
    """Everything the four modes need, bound to ONE active build (DR-001).

    The caller mints all stores off the same active-build resolution; the
    router re-verifies the scopes agree before mixing their outputs (DR-006).
    When the deps share one Postgres connection, mint ``sql_reader`` FIRST —
    its loaned-clean contract (C6b) refuses a connection another factory's
    lookup has already begun a transaction on. At RUN time the sql mode's
    per-phase transactions will also ROLL BACK any read transaction a prior
    mode's fetch auto-began on that shared connection — harmless for these
    read-only modes, but wiring that interleaves hybrid with WRITES on the
    same connection must give the sql reader its own connection instead.
    """

    repo: BuildScopedRepo
    vectors: BuildScopedVectorRepo
    embedder: BaseEmbedding
    sql_reader: BuildScopedSqlReader
    graph: BuildScopedGraphRepo
    llm: LLM


@dataclass(frozen=True)
class HybridPolicy:
    """The resolved policy slice hybrid consumes.

    ``top_k``/``max_sql_rows`` are caller-reconciled ceilings (the C6b
    contract); ``expose_debug`` gates the §16 debug block (§21 — the caller
    has already combined the policy flag with the caller's role).
    """

    text_to_sql: TextToSql
    text_to_cypher: TextToCypher
    max_graph_hops: int
    top_k: int
    max_sql_rows: int
    expose_debug: bool
    #: the WHOLE-call wall-clock budget (§21 max_latency_ms): per-mode DB
    #: timeouts alone don't bound the request — modes run sequentially and
    #: selector/embedding work carries no DB deadline, so the router enforces
    #: one shared deadline across everything it does
    max_latency_ms: int = 30_000


async def hybrid_query(
    deps: HybridDeps,
    policy: HybridPolicy,
    query: str,
    graph_params: GraphQueryParams | None = None,
) -> McpResponse:
    """§8 hybrid retrieval over the active build, as a §16 response.

    ``graph_params`` is the caller's optional graph invocation (template +
    seed); without it the graph mode is skipped with a reason — the router
    never fabricates traversal parameters from prose.
    """
    started = time.monotonic()
    _check_scopes(deps)
    if type(policy.top_k) is not int or policy.top_k < 1:
        warning = QueryWarning(
            "GUARDRAIL_BLOCKED", f"top_k must be a positive integer, got {policy.top_k!r}"
        )
        return _response(deps, query, (), (warning,), None)

    available, gated = _available_modes(policy, graph_params)

    # ONE wall-clock deadline for the WHOLE call (§21 max_latency_ms): the
    # per-mode DB timeouts are already clamped to it, but modes run
    # SEQUENTIALLY and the selector/embedding work has no DB deadline — so
    # every stage below runs on the remaining budget (the C6b/C6c per-phase
    # lesson, applied to the router itself).
    deadline = started + policy.max_latency_ms / 1000.0

    def _remaining() -> float:
        return deadline - time.monotonic()

    try:
        async with asyncio.timeout(max(_remaining(), 0.001)):
            selected, unselected, reason = await _select_modes(deps.llm, query, available)
    except TimeoutError:
        selected, unselected, reason = (
            list(available),
            [],
            "selector timed out — ran every available mode",
        )

    mode_responses: dict[str, McpResponse] = {}
    warnings: list[QueryWarning] = []
    deadline_cut: list[str] = []
    for mode in selected:
        remaining = _remaining()
        if remaining <= 0:
            deadline_cut.append(mode)
            continue
        try:
            async with asyncio.timeout(remaining):
                mode_responses[mode] = await _run_mode(mode, deps, policy, query, graph_params)
        except TimeoutError:
            deadline_cut.append(mode)
        except Exception as exc:  # noqa: BLE001 — §22: one store down ≠ hybrid down
            warnings.append(
                QueryWarning(
                    "STORE_UNAVAILABLE",
                    f"{mode} mode failed ({type(exc).__name__}) — degraded to the "
                    "remaining modes (§22)",
                )
            )
    if deadline_cut:
        warnings.append(
            QueryWarning(
                "PARTIAL_RESULTS",
                f"query exceeded the {policy.max_latency_ms}ms deadline — "
                f"mode(s) {deadline_cut} did not complete (§21/§22)",
            )
        )

    for mode, response in mode_responses.items():
        for mode_warning in response.warnings:
            warnings.append(QueryWarning(mode_warning.code, f"[{mode}] {mode_warning.message}"))
    for mode, why in gated:
        warnings.append(QueryWarning("MODE_SKIPPED", f"{mode} mode skipped — {why}"))

    fused, truncated = _fuse(
        [mode_responses[mode].results for mode in _MODE_ORDER if mode in mode_responses],
        policy.top_k,
    )
    if truncated:
        warnings.append(
            QueryWarning("TRUNCATED", f"result truncated to the top_k={policy.top_k} ceiling (§21)")
        )

    debug: dict[str, Any] | None = None
    if policy.expose_debug:
        ran = [mode for mode in selected if mode in mode_responses]  # completed only
        stores: list[str] = []
        for mode in ran:
            for store in _MODE_STORES[mode]:
                if store not in stores:
                    stores.append(store)
        debug = {
            "stores_used": stores,
            "retrieval_plan": [
                f"{mode}: {len(mode_responses[mode].results)} result(s)" for mode in ran
            ],
            "routing_decision": {
                "selected": list(selected),
                "skipped": [mode for mode, _ in gated] + list(unselected),
                "reason": reason,
            },
            "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
        }

    return _response(deps, query, fused, tuple(warnings), debug)


def _check_scopes(deps: HybridDeps) -> None:
    """All stores must be bound to the SAME (project, build) — fusion mixes
    their outputs, and a split scope would cross builds (DR-006)."""
    scopes = {
        (deps.repo.project, deps.repo.build_id),
        (deps.vectors.project, deps.vectors.build_id),
        (deps.sql_reader.project, deps.sql_reader.build_id),
        (deps.graph.project, deps.graph.build_id),
    }
    if len(scopes) != 1:
        raise ValueError(
            f"hybrid deps are bound to different scopes {sorted(scopes, key=str)} — "
            "fusion would mix builds (DR-006)"
        )


def _available_modes(
    policy: HybridPolicy, graph_params: GraphQueryParams | None
) -> tuple[list[str], list[tuple[str, str]]]:
    """The modes this request CAN run, and the (mode, reason) pairs it can't.

    Gating happens BEFORE selection so the selector can never pick a mode the
    policy forbids or the request cannot parameterize."""
    available: list[str] = []
    gated: list[tuple[str, str]] = []
    for mode in _MODE_ORDER:
        if mode == "sql" and not policy.text_to_sql.enabled:
            gated.append((mode, "sql mode is disabled by policy"))
        elif mode == "graph" and graph_params is None:
            gated.append((mode, "no graph parameters supplied (template + seed entity)"))
        else:
            available.append(mode)
    return available, gated


async def _select_modes(
    llm: LLM, query: str, available: list[str]
) -> tuple[list[str], list[str], str | None]:
    """LLM-assisted mode selection over the AVAILABLE set.

    Returns ``(selected, unselected, reason)`` in fixed mode order. The
    answer is untrusted (C3b): any failure — transport, parse, shape,
    out-of-vocabulary, empty intersection — selects EVERYTHING available,
    with the failure named in the reason (breadth over silence, §22)."""
    if len(available) <= 1:
        return list(available), [], "single available mode — selector not consulted"
    try:
        answer = await llm.achat(
            [
                ChatMessage(role=MessageRole.SYSTEM, content=_SELECTOR_SYSTEM),
                ChatMessage(
                    role=MessageRole.USER,
                    content=json.dumps({"question": query, "available": available}),
                ),
            ]
        )
        payload = _parse_selection(answer.message.content or "")
        picked = [mode for mode in available if mode in payload["modes"]]
        extras = [mode for mode in payload["modes"] if mode not in available]
        if extras:
            # a MIXED answer (valid + hallucinated/unavailable modes) is not
            # half-trusted: one out-of-vocabulary member marks the whole
            # selection unreliable, and honoring the valid half would silently
            # narrow retrieval — the documented failure rule (any
            # out-of-vocabulary output → breadth) applies to the whole answer
            return (
                list(available),
                [],
                f"selector named unavailable mode(s) {extras} — ran every available mode",
            )
        if not picked:
            return (
                list(available),
                [],
                "selector picked no available mode — ran every available mode",
            )
        reason = payload["reason"]
        return picked, [mode for mode in available if mode not in picked], reason
    except Exception:  # noqa: BLE001 — a broken selector must not silence modes (§22)
        return list(available), [], "selector failed — ran every available mode"


def _parse_selection(text: str) -> dict[str, Any]:
    """Strictly parse the selector's JSON (C3b value tree: absent field, wrong
    type, wrong item types all raise — the caller falls back to breadth)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("selection must be a JSON object")
    modes = payload.get("modes")
    if not isinstance(modes, list) or not all(isinstance(mode, str) for mode in modes):
        raise ValueError("modes must be a list of strings")
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string or null")
    return {"modes": modes, "reason": reason}


async def _run_mode(
    mode: str,
    deps: HybridDeps,
    policy: HybridPolicy,
    query: str,
    graph_params: GraphQueryParams | None,
) -> McpResponse:
    if mode == "semantic":
        return await semantic_search(deps.repo, deps.vectors, deps.embedder, query, policy.top_k)
    if mode == "graph":
        assert graph_params is not None  # gated in _available_modes
        return await graph_query(
            deps.graph, deps.repo, policy.text_to_cypher, graph_params, query, policy.max_graph_hops
        )
    if mode == "sql":
        return await sql_query(
            deps.sql_reader, deps.llm, policy.text_to_sql, query, policy.max_sql_rows
        )
    assert mode == "global"
    return await global_summary(deps.repo, query, policy.top_k)


def _fuse(
    result_lists: list[tuple[RetrievalResult, ...]], top_k: int
) -> tuple[tuple[RetrievalResult, ...], bool]:
    """Reciprocal-rank fusion across mode result lists (k=60).

    Mode scores are incomparable (cosine vs positional) — RANKS are the shared
    currency: ``score(d) = Σ 1/(k + rank_mode(d))``. Duplicates (same
    result_type + id) accumulate rank contributions and merge their
    source_refs (first mode's payload wins; mode order is fixed, so the merge
    is deterministic). Clipped to ``top_k`` (TRUNCATED reported by caller)."""
    scores: dict[tuple[str, str], float] = {}
    first: dict[tuple[str, str], RetrievalResult] = {}
    merged_refs: dict[tuple[str, str], list[SourceRef]] = {}
    for results in result_lists:
        for rank, result in enumerate(results, start=1):
            key = (result.result_type, result.id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            if key not in first:
                first[key] = result
                merged_refs[key] = list(result.source_refs)
            else:
                seen = {(ref.source_type, ref.id) for ref in merged_refs[key]}
                merged_refs[key].extend(
                    ref for ref in result.source_refs if (ref.source_type, ref.id) not in seen
                )
    fused = [
        RetrievalResult(
            result_type=base.result_type,
            id=base.id,
            score=scores[key],
            source_refs=tuple(merged_refs[key]),
            title=base.title,
            text=base.text,
            confidence=base.confidence,
        )
        for key, base in first.items()
    ]
    ordered = ordered_results(fused)
    return ordered[:top_k], len(ordered) > top_k


def _response(
    deps: HybridDeps,
    query: str,
    results: tuple[RetrievalResult, ...],
    warnings: tuple[QueryWarning, ...],
    debug: dict[str, Any] | None,
) -> McpResponse:
    return McpResponse(
        query=query,
        tool=_TOOL,
        project=deps.repo.project,
        build_id=str(deps.repo.build_id),
        results=results,
        warnings=warnings,
        debug=debug,
    )
