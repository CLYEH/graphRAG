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
from typing import Any

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

    def to_metrics_payload(self) -> dict[str, Any]:
        """The shape stored at builds.metrics['eval'] and read by the §14
        preflight gate + Health (§19)."""
        return {
            "score": self.score,
            "passed": self.passed,
            "failed": self.failed,
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
      PLUS a 1-hop neighbors walk around its src — shortest_path is untyped
      (any active edge between the endpoints can come back), while the
      neighborhood's direct edges carry every TYPE between the pair as
      rendered relation results;
    - every expected entity → its own 1-hop neighbors walk (an entity off
      the relation paths would otherwise never be retrieved).

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
        _add(GraphQueryParams(template="neighbors", entity=rel["src"], hops=1))
    for name in case.expects.get("must_contain_entities", []):
        _add(GraphQueryParams(template="neighbors", entity=name, hops=1))
    return derived


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
    embedder: BaseEmbedding,
    llm: LLM,
    project: str,
    build_id: uuid.UUID,
    golden: GoldenSet,
    policy: QueryPolicy,
) -> EvalReport:
    """Score ``build_id`` (ready or active) against the golden set and write
    the report to ``builds.metrics['eval']``."""
    binding = await resolve_eval_binding(conn, project, build_id)
    await conn.rollback()  # loaned-clean for the sql reader (C6b)
    deps = HybridDeps(
        repo=BuildScopedRepo.bound_to(conn, binding),
        vectors=BuildScopedVectorRepo.bound_to(qdrant, binding),
        embedder=embedder,
        sql_reader=BuildScopedSqlReader.bound_to(conn, binding),
        graph=BuildScopedGraphRepo.bound_to(graph_session, binding),
        llm=llm,
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
    )

    await conn.rollback()  # end any read txn before OUR write txn
    async with conn.begin():
        await conn.execute(
            tables.builds.update()
            .where(tables.builds.c.id == build_id)
            .values(
                metrics=sa.func.coalesce(tables.builds.c.metrics, sa.text("'{}'::jsonb")).op("||")(
                    sa.cast({"eval": report.to_metrics_payload()}, postgresql.JSONB)
                )
            )
        )
    return report
