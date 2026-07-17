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
- a matched name that is a substring of ANOTHER matched name is shadowed by
  it (「區域」 yields to 「區域探索廳」): the longest name is the most
  specific claim about what the question means.
- order = first occurrence in the question, longer name first on ties — for
  the path template this reads as src → dst in question order (「從 A 到 B」
  links A before B).

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
    candidates: list[tuple[int, int, str, str]] = []
    for stored in names:
        normalized = norm_text(stored)
        if len(normalized) < MIN_LINK_LENGTH:
            continue
        position = qnorm.find(normalized)
        if position >= 0:
            candidates.append((position, -len(normalized), normalized, stored))
    kept = [
        candidate
        for candidate in candidates
        if not any(candidate[2] != other[2] and candidate[2] in other[2] for other in candidates)
    ]
    kept.sort()
    linked: list[str] = []
    seen: set[str] = set()
    for _, _, normalized, stored in kept:
        if normalized in seen:
            continue  # two stored spellings normalize equal — first (earliest) wins
        seen.add(normalized)
        linked.append(stored)
    return linked


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
