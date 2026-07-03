"""Observability item_ref rules (DESIGN §18/§27.7, Track 0 P6).

The three-layer tables (``pipeline_runs/steps/items``, core.stores.tables)
only deliver §18's promise — "重跑對得上", reruns line up — if every producer
identifies work items the same way. This module freezes that identification:

1. **Stable item_ref keys** (§18) — per item_kind, the identifier that stays
   put when a build re-executes: ``document → content_hash``,
   ``entity → entity_key``. A run-local id (row pk, list index) would make
   "retry the failed items" unanswerable across runs. The mapping is a frozen
   *minimum*: new kinds are added as their pipelines land (C2+), existing
   kinds are never repointed.
2. **Item statuses, frozen minimum** (§4/§18) — default verbosity records
   exactly ``failed``/``skipped`` items. The vocabulary may grow (sampled/all
   verbosity records successes) but these two never rename: the retry
   boundary below matches ``failed`` verbatim, so a renamed status would
   silently empty every retry.
3. **Retry-failed-only boundary** (§27.7) — the retry input is the previous
   run's failed item set, deduped by (item_kind, item_ref); the output merges
   back into the *same* build_id. ``retry_failed_only`` is that rule as code.

§27.7's other rule — ingest runs always carry the building build's id, only
pure source-validation jobs may have ``build_id = NULL`` — is enforced for
the one kind §27.7 names by the ``pipeline_runs_ingest_has_build`` CHECK
(``INGEST_RUN_KIND`` is its single source); run kinds are otherwise open
vocabulary (the frozen §15 contract keeps ``Job.kind`` a free string), so the
general binding stays a writers' contract (C2/BA1/BA2).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

#: §18 verbatim: item_kind → the stable identifier used as item_ref. Extend
#: per kind as pipelines land; never repoint an existing kind (that would
#: break rerun line-up for already-recorded items).
ITEM_REF_STABLE_KEYS_MIN: Mapping[str, str] = MappingProxyType(
    {
        "document": "content_hash",
        "entity": "entity_key",
    }
)

#: The status the §27.7 retry boundary selects on — verbatim in §4/§22
#: ("該項記 pipeline_step_items failed").
ITEM_STATUS_FAILED = "failed"

#: §4/§18 frozen minimum item-status vocabulary (default verbosity records
#: exactly these). Extend, never rename.
ITEM_STATUSES_MIN = (ITEM_STATUS_FAILED, "skipped")

#: §27.7 verbatim: the one run kind whose build binding is frozen (ingest
#: always attaches to the building build). Mirrored by the
#: ``pipeline_runs_ingest_has_build`` CHECK; a schema test pins the lockstep.
INGEST_RUN_KIND = "ingest"


@dataclass(frozen=True)
class ItemOutcome:
    """One ``pipeline_step_items`` row as the retry boundary sees it."""

    item_kind: str
    item_ref: str
    status: str


def retry_failed_only(items: Iterable[ItemOutcome]) -> frozenset[tuple[str, str]]:
    """§27.7: the retry input — the previous run's failed items, deduped.

    Returns the (item_kind, item_ref) pairs eligible for re-processing. The
    set semantics ARE the idempotency guarantee: an item that failed in
    several steps (or appears twice across partial retries) re-enters exactly
    once, so retrying failures never fans out duplicated work. item_kind is
    part of the identity — a document and an entity may legitimately share a
    ref string without colliding. Anything not ``failed`` (skipped, or the
    successes sampled/all verbosity records) never re-enters.
    """
    return frozenset(
        (item.item_kind, item.item_ref) for item in items if item.status == ITEM_STATUS_FAILED
    )
