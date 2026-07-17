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
- a match whose edge glues onto an adjacent ASCII word character is not a
  mention (「us」 inside 「business」): Latin/digit edges require a word
  boundary. Deliberately ASCII-scoped — CJK has no word boundaries, and
  ``str.isalnum()`` (True for 區) would wrongly reject every CJK containment.
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

import asyncio
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.query.graph import GraphQueryParams
from core.resolve.fingerprints import norm_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.stores.repo import BuildScopedRepo

#: linking never considers normalized names shorter than this
MIN_LINK_LENGTH = 2

#: the occurrence scan yields to the event loop every this-many names, so the
#: caller's asyncio.timeout can actually preempt a large dictionary (a pure
#: CPU loop with no awaits is invisible to cancellation — Codex #89 R2)
SCAN_YIELD_EVERY = 512

#: qnorm is casefolded, so lowercase + digits cover the ASCII word alphabet
_ASCII_WORD = frozenset(string.ascii_lowercase + string.digits)


@dataclass(frozen=True)
class GraphPlan:
    """A derived graph invocation plus its audit trail for the routing trace."""

    params: GraphQueryParams
    linked_names: tuple[str, ...]  # stored canonical names, question order
    note: str  # one human-readable line for debug.retrieval_plan


def _word_bounded(qnorm: str, start: int, end: int) -> bool:
    """False when the match glues onto an adjacent ASCII word character —
    「us」 inside 「business」 is not a mention of US. Scoped to the ASCII
    alphabet on purpose: CJK has no word boundaries (str.isalnum() is True
    for 區, which would wrongly reject every CJK containment), so only
    Latin/digit-edged matches need a boundary."""
    if start > 0 and qnorm[start - 1] in _ASCII_WORD and qnorm[start] in _ASCII_WORD:
        return False
    return not (end < len(qnorm) and qnorm[end - 1] in _ASCII_WORD and qnorm[end] in _ASCII_WORD)


def _occurrences(qnorm: str, stored_names: Sequence[str]) -> list[tuple[int, int, str, str]]:
    """EVERY word-bounded occurrence of every eligible name — span shadowing
    needs them all. Pure; (start, end, norm, stored) tuples."""
    found: list[tuple[int, int, str, str]] = []
    for stored in stored_names:
        normalized = norm_text(stored)
        if len(normalized) < MIN_LINK_LENGTH:
            continue
        start = qnorm.find(normalized)
        while start >= 0:
            end = start + len(normalized)
            if _word_bounded(qnorm, start, end):
                found.append((start, end, normalized, stored))
            start = qnorm.find(normalized, start + 1)
    return found


def _claim(occurrences: list[tuple[int, int, str, str]]) -> list[str]:
    """Longest-first span claiming: each question character cites at most one
    entity, so a name links iff SOME occurrence survives the longer names'
    claims — every occurrence of a linked name still claims its span, or a
    sub-name could sneak in through a later duplicate mention. Pure."""
    claimed: list[tuple[int, int]] = []
    first_claim: dict[str, tuple[int, int, str]] = {}  # norm → (pos, -len, stored)
    for start, end, normalized, stored in sorted(
        occurrences, key=lambda o: (o[0] - o[1], o[0], o[2], o[3])
    ):
        if any(s < end and start < e for s, e in claimed):
            continue
        claimed.append((start, end))
        if normalized not in first_claim:
            # normalize-equal stored spellings collapse to the first claim
            first_claim[normalized] = (start, start - end, stored)
    return [stored for _, _, stored in sorted(first_claim.values())]


def link_names(question: str, names: Sequence[str]) -> list[str]:
    """The stored entity names the question mentions, in question order.

    Pure and deterministic — see the module docstring for the rules. The
    async router path composes the same two stages with yield points
    (:func:`plan_graph_query`); this synchronous composition exists for tests
    and any caller that already holds the name list.
    """
    qnorm = norm_text(question)
    if not qnorm:
        return []
    return _claim(_occurrences(qnorm, names))


def _plan_from_linked(linked: list[str], max_graph_hops: int) -> GraphPlan | None:
    """Template selection over the linked names — the ONE rule both the sync
    and the yielding async compositions share."""
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


def derive_plan(question: str, names: Sequence[str], max_graph_hops: int) -> GraphPlan | None:
    """A safe graph plan for ``question``, or None when nothing links."""
    return _plan_from_linked(link_names(question, names), max_graph_hops)


async def plan_graph_query(
    repo: BuildScopedRepo, question: str, max_graph_hops: int
) -> GraphPlan | None:
    """Link ``question`` against the build's active entity names (one scoped
    SELECT) and derive the plan. The names come from the same SoR that will
    resolve the seeds, so an emitted plan is resolvable by construction.

    The scan yields to the event loop every :data:`SCAN_YIELD_EVERY` names —
    the SAME pure stages ``link_names`` composes, chunked so the router's
    shared-deadline ``asyncio.timeout`` can preempt a large dictionary
    instead of the CPU loop blocking the loop past ``max_latency_ms``."""
    names = await repo.distinct_active_entity_names()
    qnorm = norm_text(question)
    if not qnorm:
        return None
    occurrences: list[tuple[int, int, str, str]] = []
    for offset in range(0, len(names), SCAN_YIELD_EVERY):
        occurrences.extend(_occurrences(qnorm, names[offset : offset + SCAN_YIELD_EVERY]))
        await asyncio.sleep(0)  # the cancellation point the timeout needs
    linked = _claim(occurrences)
    return _plan_from_linked(linked, max_graph_hops)
