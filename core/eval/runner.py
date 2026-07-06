"""The eval runner (§20/§27.5; C10): score one build against the golden set.

Wiring mirrors the MCP context: long-lived engines are the CALLER's; the
runner binds every store to the NAMED build via the §20 eval binding (a
ready candidate or the active build — the activation gate must score builds
BEFORE they serve). Each case dispatches to its frozen mode:

- semantic/sql/global/hybrid run their C6 functions directly on the bound
  stores with the project's reconciled policy;
- graph derives its §27.6 template parameters from the case's expectations
  (golden cases carry question+mode only): a ``must_include_relations``
  expectation drives a ``path`` between its src/dst; otherwise the first
  ``must_contain_entities`` name drives ``neighbors``. No derivable anchor →
  the case scores 0 with a loud reason (never silently skipped, Rule 12).

Per-case subscores come from :mod:`core.eval.scoring` (pure); path_validity
is computed here (it needs the SoR to resolve per-edge relation refs).
``answer_similarity`` is not emitted — the frozen golden schema carries no
reference answer (documented in scoring.py).

The report is written to ``builds.metrics['eval']`` (SoR-attached, so the
§14 preflight eval gate and Health §19 read the same numbers the runner
produced — one producer, one location).
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import Any, cast

import sqlalchemy as sa
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms import LLM
from neo4j import AsyncSession
from qdrant_client import AsyncQdrantClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection

from core.eval import scoring
from core.eval.golden import GoldenCase, GoldenSet
from core.mcp.policy import QueryPolicy, hybrid_policy
from core.query.global_reports import global_summary
from core.query.graph import GraphQueryParams, graph_query
from core.query.hybrid import HybridDeps, hybrid_query
from core.query.results import McpResponse, QueryWarning, RetrievalResult
from core.query.semantic import semantic_search
from core.query.sql import sql_query
from core.stores import tables
from core.stores.graph import BuildScopedGraphRepo
from core.stores.repo import BuildScopedRepo, resolve_eval_binding
from core.stores.sqlreader import BuildScopedSqlReader
from core.stores.vectors import BuildScopedVectorRepo


def models_needed(golden: GoldenSet, policy: QueryPolicy) -> tuple[bool, bool]:
    """(needs_embedder, needs_llm) for this golden set UNDER this policy —
    graph/global cases touch neither model client; semantic needs only the
    embedder; hybrid needs both (its selector prompts the LLM and its
    semantic mode embeds; semantic+global are never gated, so the selector
    is always consulted). An ``sql`` case needs the LLM only when the
    policy actually ENABLES text_to_sql — disabled, sql_query returns
    MODE_SKIPPED before touching the model, and a keyless project must
    still be able to score and persist that (failing) report rather than
    be refused into staying unscored."""
    needs_embedder = any(case.mode in ("semantic", "hybrid") for case in golden.cases)
    needs_llm = any(case.mode == "hybrid" for case in golden.cases) or (
        policy.text_to_sql.enabled and any(case.mode == "sql" for case in golden.cases)
    )
    return needs_embedder, needs_llm


def eval_fingerprint(golden: GoldenSet, policy: QueryPolicy) -> str:
    """Identity of WHAT was evaluated (§20): the golden cases + the policy
    values that shape scoring. Two reports are comparable only when their
    fingerprints match — a candidate scored against a different (easier)
    golden set or laxer policy must not pass the regression gate on raw
    numbers. Canonical JSON (sorted keys) over dataclass dumps → sha256."""
    import dataclasses
    import hashlib
    import json

    def _dump(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        # non-dataclass doubles (tests) — deterministic best-effort dump
        return (
            {k: str(v) for k, v in sorted(vars(obj).items())}
            if hasattr(obj, "__dict__")
            else str(obj)
        )

    document = {
        "cases": [dataclasses.asdict(case) for case in golden.cases],
        "policy": _dump(policy),
    }
    canonical = json.dumps(document, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CaseResult:
    question: str
    mode: str
    score: float
    passed: bool
    subscores: dict[str, float]
    note: str | None = None


@dataclass(frozen=True)
class EvalReport:
    build_id: uuid.UUID
    score: float
    passed: int
    failed: int
    cases: tuple[CaseResult, ...]
    metrics: dict[str, float]
    fingerprint: str

    def to_metrics_payload(self) -> dict[str, Any]:
        """The shape stored at builds.metrics['eval'] and read by the §14
        preflight gate + Health (§19)."""
        return {
            "score": self.score,
            "passed": self.passed,
            "failed": self.failed,
            "fingerprint": self.fingerprint,
            "metrics": self.metrics,
            "cases": [
                {
                    "question": c.question,
                    "mode": c.mode,
                    "score": c.score,
                    "passed": c.passed,
                }
                for c in self.cases
            ],
        }


def _derive_graph_params(case: GoldenCase, max_hops: int) -> list[GraphQueryParams]:
    """EVERYTHING the case scores over gets queried — score_case computes
    each metric over its WHOLE expectation list, so any expectation the
    runner never fetches under-scores builds that actually satisfy it:

    - every expected relation → a path query (connectivity, path_validity)
      PLUS a 1-hop SUBGRAPH around its src — shortest_path is untyped (any
      active edge between the endpoints can come back), and of the §27.6
      templates only subgraph EMITS relation results (rendered
      "src -[type]-> dst"), exposing every type between the pair;
    - every expected entity → its own 1-hop subgraph: subgraph INCLUDES the
      seed (a singleton/disconnected expected entity must be retrievable by
      its own name — the neighbors template excludes the seed and would
      score 0 on exactly the entity the case names).

    Queries are deduped by parameters; N queries per case is fine for an
    offline eval harness."""
    derived: list[GraphQueryParams] = []
    seen: set[tuple[str, str, str | None, int]] = set()

    def _add(params: GraphQueryParams) -> None:
        key = (params.template, params.entity, params.other_entity, params.hops)
        if key not in seen:
            seen.add(key)
            derived.append(params)

    for rel in case.expects.get("must_include_relations", []):
        _add(
            GraphQueryParams(
                template="path", entity=rel["src"], other_entity=rel["dst"], hops=max_hops
            )
        )
        _add(GraphQueryParams(template="subgraph", entity=rel["src"], hops=1))
    for name in case.expects.get("must_contain_entities", []):
        _add(GraphQueryParams(template="subgraph", entity=name, hops=1))
    return derived


async def _expected_edges(
    repo: Any, graph: Any, policy: QueryPolicy, case: GoldenCase
) -> McpResponse | None:
    """Targeted lookup for the case's expected typed edges (§27.2-cited).

    The derived queries can miss the expected type through no fault of the
    build: shortest_path is untyped, and a dense 1-hop subgraph spends its
    row budget entities-first, starving the edge stage. The expectation
    names an exact (src, dst, type) — verified against BOTH stores: the SoR
    must hold the active, evidence-cited relation AND the graph projection
    must hold the same typed edge (eval scores what the production graph
    tool could actually retrieve — a count-balanced but wrong-edge
    projection must not pass on Postgres alone). Nothing is synthesized for
    edges either store lacks (retrieval widens, truth does not)."""
    from core.query.graph import evidence_ref

    expectations = case.expects.get("must_include_relations")
    if not expectations:
        return None
    results: list[RetrievalResult] = []
    for expectation in expectations:
        src_ids = await repo.entity_ids_by_name(expectation["src"])
        dst_ids = await repo.entity_ids_by_name(expectation["dst"])
        triples = [
            (src_id, dst_id, expectation["type"]) for src_id in src_ids for dst_id in dst_ids
        ]
        if not triples:
            continue
        resolved = await repo.relations_with_evidence(triples)
        if not resolved:
            continue
        # the PROJECTION must hold the same typed edge — §19 counts alone
        # cannot see a wrong-type/stale edge
        endpoint_ids = sorted({str(t[0]) for t in resolved} | {str(t[1]) for t in resolved})
        projected = await graph.edges_among(
            endpoint_ids,
            limit=policy.cypher_policy().max_rows,
            timeout_ms=policy.cypher_policy().timeout_ms,
        )
        projected_triples = {
            (edge.get("src"), edge.get("dst"), edge.get("type")) for edge in projected
        }
        for triple, (relation_id, evidence_rows) in resolved.items():
            if (str(triple[0]), str(triple[1]), triple[2]) not in projected_triples:
                continue  # SoR holds it, the projection does not — not retrievable
            refs = tuple(ref for row in evidence_rows if (ref := evidence_ref(row)) is not None)
            if not refs:
                continue  # §27.2: a relation result cites ≥1 evidence
            results.append(
                RetrievalResult(
                    result_type="relation",
                    id=str(relation_id),
                    score=1.0,
                    source_refs=refs,
                    title=(f"{expectation['src']} -[{expectation['type']}]-> {expectation['dst']}"),
                )
            )
    if not results:
        return None
    return McpResponse(
        query=case.question,
        tool="graph_query",
        project=repo.project,
        build_id=str(repo.build_id),
        results=tuple(results),
        warnings=(),
    )


async def _path_validity(repo: BuildScopedRepo, response: McpResponse) -> float | None:
    """Share of path results whose per-edge relation refs all resolve to
    ACTIVE relations in the SoR. None when the response has no path results
    (the caller decides what an asserted-but-absent path means)."""
    paths = [r for r in response.results if r.result_type == "path"]
    if not paths:
        return None
    edge_ids: set[uuid.UUID] = set()
    for path in paths:
        for ref in path.source_refs:
            if ref.source_type == "relation":
                # a non-uuid ref cannot resolve → it counts as invalid below
                with contextlib.suppress(ValueError):
                    edge_ids.add(uuid.UUID(ref.id))
    known: set[uuid.UUID] = set()
    if edge_ids:
        rows = await repo.fetch_all(
            tables.relations,
            tables.relations.c.id.in_(list(edge_ids)),
            tables.relations.c.status == "active",
        )
        known = {row.id for row in rows}
    valid = 0
    for path in paths:
        refs = [ref for ref in path.source_refs if ref.source_type == "relation"]
        ok = bool(refs)
        for ref in refs:
            try:
                if uuid.UUID(ref.id) not in known:
                    ok = False
            except ValueError:
                ok = False
        valid += 1 if ok else 0
    return valid / len(paths)


async def _run_case(
    deps: HybridDeps, policy: QueryPolicy, case: GoldenCase
) -> tuple[McpResponse | None, str | None]:
    if case.mode == "semantic":
        return (
            await semantic_search(
                deps.repo, deps.vectors, deps.embedder, case.question, policy.top_k(None)
            ),
            None,
        )
    if case.mode == "sql":
        return (
            await sql_query(
                deps.sql_reader, deps.llm, policy.sql_policy(), case.question, policy.sql_rows()
            ),
            None,
        )
    if case.mode == "global":
        return await global_summary(deps.repo, case.question, policy.top_k(None)), None
    if case.mode == "graph":
        param_list = _derive_graph_params(case, policy.max_graph_hops)
        if not param_list:
            return None, (
                "graph case has no derivable anchor — add must_include_relations "
                "or must_contain_entities to drive the §27.6 template"
            )
        responses = [
            await graph_query(
                deps.graph,
                deps.repo,
                policy.cypher_policy(),
                params,
                case.question,
                policy.max_graph_hops,
            )
            for params in param_list
        ]
        targeted = await _expected_edges(deps.repo, deps.graph, policy, case)
        if targeted is not None:
            responses.append(targeted)
        return _merge_responses(responses), None
    # hybrid — the default entry; the FIRST derived anchor makes the graph
    # mode available. Hybrid runs ONE query per mode by design, so a
    # multi-relation hybrid case scores relation_hit_rate against the tool's
    # genuine one-path output — deliberate fidelity to what the production
    # tool returns, NOT the under-fetch bug the graph mode fixed (fetching
    # all expected relations would OVER-score hybrid instead)
    param_list = _derive_graph_params(case, policy.max_graph_hops)
    params = param_list[0] if param_list else None
    return await hybrid_query(deps, hybrid_policy(policy, None), case.question, params), None


def _merge_responses(responses: list[McpResponse]) -> McpResponse:
    """One §16 response for scoring: results concatenated (deduped by
    (result_type, id) keeping the first), warnings unioned in order."""
    first = responses[0]
    seen: set[tuple[str, str]] = set()
    results: list[RetrievalResult] = []
    warnings: list[QueryWarning] = []
    for response in responses:
        for result in response.results:
            key = (result.result_type, result.id)
            if key not in seen:
                seen.add(key)
                results.append(result)
        warnings.extend(w for w in response.warnings if w not in warnings)
    return McpResponse(
        query=first.query,
        tool=first.tool,
        project=first.project,
        build_id=first.build_id,
        results=tuple(results),
        warnings=tuple(warnings),
    )


async def run_eval(
    conn: AsyncConnection,
    qdrant: AsyncQdrantClient,
    graph_session: AsyncSession,
    embedder: BaseEmbedding | None,
    llm: LLM | None,
    project: str,
    build_id: uuid.UUID,
    golden: GoldenSet,
    policy: QueryPolicy,
) -> EvalReport:
    """Score ``build_id`` (ready or active) against the golden set and write
    the report to ``builds.metrics['eval']``."""
    binding = await resolve_eval_binding(conn, project, build_id)
    await conn.rollback()  # loaned-clean for the sql reader (C6b)
    # None model clients are legal when no case's mode calls them
    # (models_needed) — reaching one anyway is a loud bug, never silent
    deps = HybridDeps(
        repo=BuildScopedRepo.bound_to(conn, binding),
        vectors=BuildScopedVectorRepo.bound_to(qdrant, binding),
        embedder=cast(BaseEmbedding, embedder),
        sql_reader=BuildScopedSqlReader.bound_to(conn, binding),
        graph=BuildScopedGraphRepo.bound_to(graph_session, binding),
        llm=cast(LLM, llm),
    )

    results: list[CaseResult] = []
    for case in golden.cases:
        response, note = await _run_case(deps, policy, case)
        if response is None:
            results.append(CaseResult(case.question, case.mode, 0.0, False, {}, note=note))
            continue
        validity: float | None = None
        if "must_have_valid_paths" in case.expects:
            computed = await _path_validity(deps.repo, response)
            # asserted but no paths returned → the mode was expected to
            # produce paths: score the assertion 0, never skip it silently
            validity = 0.0 if computed is None else computed
        subscores = scoring.score_case(response, case.expects, validity)
        score = scoring.case_score(subscores)
        results.append(
            CaseResult(
                case.question,
                case.mode,
                score,
                scoring.case_passed(score, case.min_score),
                subscores,
            )
        )

    total = sum(r.score for r in results) / len(results)
    passed = sum(1 for r in results if r.passed)
    metric_values: dict[str, list[float]] = {}
    for result in results:
        for key, value in result.subscores.items():
            if key == "answer_regex":
                continue  # case assertion, not a frozen aggregate metric
            metric_values.setdefault(key, []).append(value)
    metrics = {key: sum(vals) / len(vals) for key, vals in metric_values.items()}

    report = EvalReport(
        build_id=build_id,
        score=total,
        passed=passed,
        failed=len(results) - passed,
        cases=tuple(results),
        metrics=metrics,
        fingerprint=eval_fingerprint(golden, policy),
    )

    await conn.rollback()  # end any read txn before OUR write txn
    async with conn.begin():
        stored = await conn.execute(
            tables.builds.update()
            .where(tables.builds.c.id == build_id)
            .values(
                metrics=sa.func.coalesce(tables.builds.c.metrics, sa.text("'{}'::jsonb")).op("||")(
                    sa.cast({"eval": report.to_metrics_payload()}, postgresql.JSONB)
                )
            )
        )
        if stored.rowcount != 1:
            # the binding was valid at resolve time, but a concurrent prune
            # can delete a ready build before this persist — a report the
            # gate can never read must not print as success (bind-time
            # check ≠ invariant)
            raise LookupError(
                f"build {build_id} disappeared before the eval report could be "
                "stored (pruned concurrently?) — nothing persisted"
            )
    return report
