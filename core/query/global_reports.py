"""Global retrieval: community_reports → §16 response (§8/§9, C6d).

The §8 ``global`` modality: the C7 ``summarize`` step's community reports,
read straight from POSTGRES — the SoR itself, so the whole
untrusted-projection re-verification apparatus of C6a/C6c does not apply
here (there is no derived store between the data and the response). Each
report becomes one ``community_report`` result cited by its member entity
refs — the §27.2 minimum, structurally guaranteed by the table's
citeable-members CHECK (and re-checked defensively on emission: a memberless
row is dropped and surfaced, never emitted uncitable).

Ranking (v1): ``rating`` descending (unrated last), id as the deterministic
tiebreak — the LLM's own importance signal from §4. The query text is echoed
in the envelope but does not rank v1 results: community reports are
build-wide summaries (the C6e hybrid router decides when the global mode is
the right answer for a query; relevance fusion lives there).

Failure is degradation (§22): an out-of-contract ``top_k`` degrades to a
typed ``GUARDRAIL_BLOCKED``; the ``top_k`` ceiling clips with ``TRUNCATED``.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from core.query.policy import GUARDRAIL_WARNING_CODE
from core.query.results import (
    McpResponse,
    QueryWarning,
    RetrievalResult,
    SourceRef,
    ordered_results,
)
from core.stores import tables
from core.stores.repo import BuildScopedRepo

_TOOL = "global_summary"


async def global_summary(repo: BuildScopedRepo, query: str, top_k: int) -> McpResponse:
    """§8 global retrieval over the active build, as a §16 response.

    ``repo`` is bound to the active build (DR-001); ``top_k`` is the
    caller-reconciled result ceiling (``min`` of the request's ``top_k`` and
    the policy's ``max_top_k`` — the same caller-reconciliation contract as
    C6b's ``max_rows``).
    """
    if type(top_k) is not int or top_k < 1:
        # bool <: int is annotation-silent and a str would break comparisons —
        # out-of-contract input degrades typed (§22), it does not 500
        return _response(
            repo,
            query,
            (),
            (
                QueryWarning(
                    GUARDRAIL_WARNING_CODE,
                    f"top_k must be a positive integer, got {top_k!r}",
                ),
            ),
        )

    rows = await repo.fetch_all(tables.community_reports)
    # rating desc with unrated LAST, then id — deterministic under any fetch
    # order (the #34 lesson: emitted order must be a pure function of the set)
    ordered = sorted(
        rows,
        key=lambda row: (row.rating is None, -(row.rating or 0.0), str(row.id)),
    )

    # citability is judged over EVERY row (not just up to the ceiling): a
    # memberless row past the break would otherwise count as "clipped" and
    # over-fire TRUNCATED — the flag must be exact in both directions (§22)
    citable: list[RetrievalResult] = []
    dropped = 0
    for row in ordered:
        result = _report_result(row)
        if result is None:
            dropped += 1  # defensively: a memberless row cannot cite (§27.2)
        else:
            citable.append(result)

    emitted = _scored(citable[:top_k])
    warnings: list[QueryWarning] = []
    if len(citable) > top_k:
        warnings.append(
            QueryWarning("TRUNCATED", f"result truncated to the top_k={top_k} ceiling (§21)")
        )
    if dropped:
        warnings.append(
            QueryWarning(
                "PARTIAL_RESULTS",
                f"{dropped} report(s) omitted — no citable members (§27.2)",
            )
        )
    return _response(repo, query, emitted, tuple(warnings))


def _report_result(row: Any) -> RetrievalResult | None:
    """One SoR report row → a §16 ``community_report`` result, or None.

    The members ARE the citation (§27.2) — the table CHECK forbids memberless
    rows, but emission re-checks rather than trusts (a hand-written or
    pre-CHECK row must drop, not crash ``RetrievalResult``'s non-empty-refs
    invariant). ``title``/``summary`` are nullable display fields — emitted
    as-is when strings, null otherwise (§16 allows null; no coerced reprs)."""
    members = [m for m in (row.member_entity_ids or []) if m is not None]
    if not members:
        return None
    refs = tuple(
        SourceRef(source_type="entity", id=str(entity_id)) for entity_id in sorted(members, key=str)
    )
    title = row.title if isinstance(row.title, str) and row.title.strip() else None
    summary = row.summary if isinstance(row.summary, str) and row.summary.strip() else None
    return RetrievalResult(
        result_type="community_report",
        id=str(row.id),
        score=0.0,  # placeholder; _scored assigns the positional value
        source_refs=refs,
        title=title,
        text=summary,
    )


def _scored(results: list[RetrievalResult]) -> tuple[RetrievalResult, ...]:
    """Positional scores — global results carry no relevance model (v1 ranks
    by rating), but a strictly descending score keeps that order through
    ``ordered_results`` (score desc)."""
    total = len(results)
    if total == 0:
        return ()
    rescored = [
        dataclasses.replace(result, score=(total - index) / total)
        for index, result in enumerate(results)
    ]
    return ordered_results(rescored)


def _response(
    repo: BuildScopedRepo,
    query: str,
    results: tuple[RetrievalResult, ...],
    warnings: tuple[QueryWarning, ...],
) -> McpResponse:
    return McpResponse(
        query=query,
        tool=_TOOL,
        project=repo.project,
        build_id=str(repo.build_id),
        results=results,
        warnings=warnings,
    )
