"""Community summarization: Leiden over the SoR graph → community_reports (§5 step 6, C7).

The pipeline's ``summarize`` step. Communities are detected over the ACTIVE
entities and relations read from POSTGRES — the SoR, never the Neo4j
projection (DR-004/DR-006: the projection is derived and forward-only-stale;
what the graph IS is Postgres's call). Each detected community is summarized
by the LLM into a ``community_reports`` row — the §8 ``global`` mode's data —
whose ``member_entity_ids`` carry the §27.2 citation minimum (community_report
→ member entity refs; the table's CHECK already refuses memberless rows).

Determinism & idempotency (§5 rerun): Leiden runs with a FIXED seed, so the
same graph yields the same partition; a community whose exact member set
already has a report in this build is skipped, so a crash-rerun writes
nothing new (the C2 skip-idempotency pattern). Failure is degradation (§22):
one community's LLM/parse failure marks THAT item ``failed`` (stable ref =
the member-set fingerprint, the §27.7 retry identity) and later communities
still run — never an aborted pass.

The LLM answer is UNTRUSTED (the C3b value tree): shape is validated inside
the per-item failure boundary over the whole envelope — absent field, wrong
type, blank strings are all the same failure mode (``failed``, retryable),
never a silently empty report and never a crashed pass.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import igraph as ig  # type: ignore[import-untyped]  # ships no stubs/py.typed
import leidenalg as la  # type: ignore[import-untyped]  # ships no stubs/py.typed
from llama_index.core.llms import LLM, ChatMessage

from core.observability.spec import ItemOutcome
from core.stores import tables
from core.stores.repo import BuildScopedWriter

#: Fixed Leiden seed — the same graph must yield the same partition, or the
#: skip-idempotency below (member-set identity) would churn on every rerun.
#: The algorithm is frozen by DESIGN §5; its LEVELS are 🔧 (v1 = single level 0).
_LEIDEN_SEED = 42

#: v1: singleton "communities" (isolated entities, or leftovers of the
#: partition) carry no relational structure worth a report — skipped, counted.
_MIN_COMMUNITY_SIZE = 2

#: Prompt-size ceilings — a huge community must not blow the context window;
#: the LLM sees a SAMPLE and the report still cites EVERY member id.
_PROMPT_MEMBER_CAP = 50
_PROMPT_RELATION_CAP = 100

_SYSTEM_PROMPT = """\
You summarize one community of related entities from a knowledge graph.
Reply with ONLY a JSON object shaped exactly:
{"title": "<short community title>", "summary": "<2-5 sentence summary>", \
"rating": <importance 0-10 or null>}
Base the summary strictly on the given members and relations; no outside facts.
"""


@dataclass(frozen=True)
class SummarizeReport:
    """The step result: communities detected (≥ min size) + written + outcomes."""

    communities: int
    written: int
    outcomes: tuple[ItemOutcome, ...]


async def summarize_build(writer: BuildScopedWriter, llm: LLM) -> SummarizeReport:
    """Detect communities over the build's active SoR graph and write reports.

    ``writer`` is bound to the building build (§27.1) — reads its own scope,
    writes ``community_reports`` with the scope injected. ``llm`` is the §3
    abstraction; one call per community so a single failure is contained.
    """
    entities = {
        row.id: row
        for row in await writer.fetch_all(tables.entities, tables.entities.c.status == "active")
    }
    relations = [
        row
        for row in await writer.fetch_all(tables.relations, tables.relations.c.status == "active")
        # endpoints must both be active — a relation kept 'active' while an
        # endpoint moved off it contributes no edge to the ACTIVE graph
        if row.src_entity_id in entities and row.dst_entity_id in entities
    ]

    communities = _leiden_communities(list(entities), relations)

    existing = {
        frozenset(row.member_entity_ids) for row in await writer.fetch_all(tables.community_reports)
    }

    outcomes: list[ItemOutcome] = []
    written = 0
    for members in communities:
        ref = _community_ref(members)
        if frozenset(members) in existing:
            outcomes.append(ItemOutcome("community", ref, "skipped"))
            continue
        try:
            answer = await llm.achat(
                [
                    ChatMessage(role="system", content=_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=_prompt(members, entities, relations)),
                ]
            )
            title, summary, rating = _parse_report(answer.message.content or "")
        except Exception:  # noqa: BLE001 — any LLM/parse failure = failed item (§22)
            outcomes.append(ItemOutcome("community", ref, "failed"))
            continue
        await writer.insert(
            tables.community_reports,
            id=uuid.uuid4(),
            level=0,  # v1 single level; hierarchy is 🔧 (§25)
            title=title,
            summary=summary,
            member_entity_ids=sorted(members, key=str),
            rating=rating,
        )
        written += 1
        outcomes.append(ItemOutcome("community", ref, "summarized"))

    return SummarizeReport(len(communities), written, tuple(outcomes))


def _leiden_communities(entity_ids: list[uuid.UUID], relations: list[Any]) -> list[list[uuid.UUID]]:
    """Leiden partition of the active graph → member-id lists (≥ min size).

    The graph is UNDIRECTED and simple (parallel/reverse edges collapse into
    one — community structure cares about connectivity, not direction), built
    over EVERY active entity so isolated ones form singletons (then dropped by
    the size floor, not silently lost from the count of considered items)."""
    if not entity_ids:
        return []
    # vertex numbering MUST be a pure function of the id SET: leidenalg is
    # vertex-order sensitive (verified: relabeling the same graph + seed can
    # change the partition), and the ids arrive in Postgres fetch order, which
    # is not stable across reruns — unsorted, a crash-retry could recompute
    # different member sets, miss the skip, and duplicate reports (§5)
    entity_ids = sorted(entity_ids, key=str)
    index = {entity_id: position for position, entity_id in enumerate(entity_ids)}
    edges = {
        (min(a, b), max(a, b))
        for row in relations
        if (a := index[row.src_entity_id]) != (b := index[row.dst_entity_id])
    }
    graph = ig.Graph(n=len(entity_ids), edges=sorted(edges))
    partition = la.find_partition(graph, la.ModularityVertexPartition, seed=_LEIDEN_SEED)
    communities = [
        sorted((entity_ids[position] for position in component), key=str) for component in partition
    ]
    return [members for members in communities if len(members) >= _MIN_COMMUNITY_SIZE]


def _community_ref(members: list[uuid.UUID]) -> str:
    """The §27.7 retry identity of one community: a digest of its SORTED member
    ids (fixed-length segments joined — no collision by construction). The ref
    only routes retry bookkeeping (accidental threat model), but sha256 is
    cheap, so no truncation."""
    joined = ",".join(str(entity_id) for entity_id in members)
    return f"community:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


def _prompt(members: list[uuid.UUID], entities: dict[uuid.UUID, Any], relations: list[Any]) -> str:
    member_set = set(members)
    listed = [
        {"name": entities[m].canonical_name, "type": entities[m].type}
        for m in members[:_PROMPT_MEMBER_CAP]
    ]
    internal = [
        {
            "src": entities[row.src_entity_id].canonical_name,
            "type": row.type,
            "dst": entities[row.dst_entity_id].canonical_name,
        }
        for row in relations
        if row.src_entity_id in member_set and row.dst_entity_id in member_set
    ][:_PROMPT_RELATION_CAP]
    return json.dumps(
        {"members": listed, "relations": internal, "member_count": len(members)},
        ensure_ascii=False,
    )


def _parse_report(text: str) -> tuple[str, str, float | None]:
    """Strictly parse the model's JSON report (fenced answers unwrapped).

    The C3b value-tree rule, inside the caller's failure boundary: an absent
    field, a wrong-typed field, or a blank title/summary are ALL the same
    failure (raise → the item is ``failed`` and retryable) — never a silently
    empty report. ``rating`` is optional: null/absent → None, but a PRESENT
    wrong-typed value (a string, a bool) is a failure, not a coercion."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("report must be a JSON object")
    title = payload.get("title")
    summary = payload.get("summary")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-blank string")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a non-blank string")
    rating = payload.get("rating")
    if rating is None:
        return title.strip(), summary.strip(), None
    if isinstance(rating, bool) or not isinstance(rating, (int, float)):
        raise ValueError("rating must be a number or null")
    if not 0 <= rating <= 10:
        # the prompt's contract is 0-10; an out-of-range value is the same
        # wrong-answer failure mode as a wrong type, not something to clamp
        raise ValueError("rating must be within 0-10")
    return title.strip(), summary.strip(), float(rating)
