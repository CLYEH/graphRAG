"""Question entity-linking → the auto graph plan (QP1, §8/§9/§21).

A bare NL question used to leave the graph mode permanently gated ("no graph
parameters supplied") — the GraphRAG core never ran for plain-language
callers (the Console 檢索測試 default; MCP agents that know no template
vocabulary). QP1 derives a SAFE plan instead of asking the caller for one:
link the question's text to the build's OWN entity names, then pick an
existing §27.6 template. The parameterized-template guardrail is untouched —
the plan only ever names a template from the frozen vocabulary plus canonical
names the SoR resolves itself (``entity_ids_by_name``), so the reachable
query surface is exactly the caller-supplied-params surface. No LLM and no
free-form Cypher anywhere in this module: linking is deterministic string
containment, auditable in the routing trace.

Matching rules (all deterministic, all surfaced in the plan note):

- both sides normalize with the frozen ``fingerprints.norm_text`` (NFKC width
  folding + casefold + whitespace collapse) — the ONE normalization the
  identity keys use; a second rule here would be checker/consumer drift by
  construction (class 5). The emitted seed is the STORED name, so downstream
  resolution (``lower()`` equality) is unaffected by how leniently we match.
- an entity name matches iff its normalized form appears inside the
  normalized question; names shorter than 2 characters never match (a
  single character matches nearly any question — noise, not a link).
- shadowing is by OVERLAPPING SPANS, not name containment: every occurrence
  of every eligible name claims its span longest-first, and each question
  character cites at most one entity — 「區域」 inside 「區域探索廳」 is
  shadowed, but a standalone 「York」 next to 「New York」 still links
  (Codex #89: name-level containment silently ate exactly the two-entity
  relation questions the path template exists for).
- order = the first claimed occurrence in the question, longer name first on
  ties — for the path template this reads as src → dst in question order
  (「從 A 到 B」 links A before B).

Template selection is conservative: two or more linked names → ``path``
between the first two (hops = the §21 ``max_graph_hops`` ceiling — a path
plan exists to FIND the relation chain, so it gets the policy's full sanctioned
depth); exactly one → ``neighbors`` at hops=1 (the cheapest sanctioned look
around the seed); zero → no plan, the graph mode stays gated with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.query.graph import GraphQueryParams
from core.resolve.fingerprints import norm_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.stores.repo import BuildScopedRepo

#: linking never considers normalized names shorter than this
MIN_LINK_LENGTH = 2


@dataclass(frozen=True)
class GraphPlan:
    """A derived graph invocation plus its audit trail for the routing trace."""

    params: GraphQueryParams
    linked_names: tuple[str, ...]  # stored canonical names, question order
    note: str  # one human-readable line for debug.retrieval_plan


def link_names(question: str, names: Sequence[str]) -> list[str]:
    """The stored entity names the question mentions, in question order.

    Pure and deterministic — see the module docstring for the rules.
    """
    qnorm = norm_text(question)
    if not qnorm:
        return []
    # EVERY occurrence of every eligible name — span shadowing needs them all
    occurrences: list[tuple[int, int, str, str]] = []  # (start, end, norm, stored)
    for stored in names:
        normalized = norm_text(stored)
        if len(normalized) < MIN_LINK_LENGTH:
            continue
        start = qnorm.find(normalized)
        while start >= 0:
            occurrences.append((start, start + len(normalized), normalized, stored))
            start = qnorm.find(normalized, start + 1)
    # longest-first claiming: each question character cites at most one
    # entity, so a name links iff SOME occurrence survives the longer names'
    # claims — every occurrence of a linked name still claims its span, or a
    # sub-name could sneak in through a later duplicate mention
    claimed: list[tuple[int, int]] = []
    first_claim: dict[str, tuple[int, int, str]] = {}  # norm → (pos, -len, stored)
    for start, end, normalized, stored in sorted(
        occurrences, key=lambda o: (o[0] - o[1], o[0], o[2])
    ):
        if any(s < end and start < e for s, e in claimed):
            continue
        claimed.append((start, end))
        if normalized not in first_claim:
            # normalize-equal stored spellings collapse to the first claim
            first_claim[normalized] = (start, start - end, stored)
    return [stored for _, _, stored in sorted(first_claim.values())]


def derive_plan(question: str, names: Sequence[str], max_graph_hops: int) -> GraphPlan | None:
    """A safe graph plan for ``question``, or None when nothing links."""
    linked = link_names(question, names)
    if not linked:
        return None
    if len(linked) >= 2:
        params = GraphQueryParams(
            template="path", entity=linked[0], other_entity=linked[1], hops=max_graph_hops
        )
        shape = f"path {linked[0]} → {linked[1]} (hops≤{max_graph_hops})"
        if len(linked) > 2:
            shape += f", +{len(linked) - 2} more linked"
    else:
        params = GraphQueryParams(template="neighbors", entity=linked[0], hops=1)
        shape = f"neighbors around {linked[0]} (hops=1)"
    note = f"graph: auto plan {shape} — linked from the question by name"
    return GraphPlan(params=params, linked_names=tuple(linked), note=note)


async def plan_graph_query(
    repo: BuildScopedRepo, question: str, max_graph_hops: int
) -> GraphPlan | None:
    """Link ``question`` against the build's active entity names (one scoped
    SELECT) and derive the plan. The names come from the same SoR that will
    resolve the seeds, so an emitted plan is resolvable by construction."""
    names = await repo.distinct_active_entity_names()
    return derive_plan(question, names, max_graph_hops)
