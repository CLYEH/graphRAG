"""Why: resolve_build's DECISION logic — which pairs merge, which become
candidates, which are suppressed, and that a second pass converges — is §7/§17
semantics that must hold independent of storage. The DB truths (unique
indexes, TOCTOU, cross-build carry) are proven in
test_resolution_integration.py; these in-memory fakes recheck the decisions
fast, and cover the auto-decision RECORDING rule: an auto-merge writes a
ledger row (decided_by='auto') so DR-003 carries it and a curator can outrank
it, while a CARRIED merge writes nothing (it is already recorded).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.resolve.fingerprints import (
    LEDGER_FINGERPRINT_VERSION,
    entity_key,
    ledger_entity_key,
    ledger_merge_key,
    ledger_relation_signature,
)
from core.resolve.resolution import ResolutionConfig, resolve_build
from core.stores import tables
from core.stores.repo import BuildScopedWriter


def _matches(row: dict[str, Any], predicate: Any) -> bool:
    clauses = getattr(predicate, "clauses", None)
    if clauses is not None:  # sa.or_
        return any(_matches(row, clause) for clause in clauses)
    return bool(row[predicate.left.name] == predicate.right.value)


class _FakeStore:
    """Shared in-memory rows for the fake writer + conn (one 'database')."""

    def __init__(self) -> None:
        self.rows: dict[Any, list[dict[str, Any]]] = {
            tables.entities: [],
            tables.relations: [],
            tables.relation_evidence: [],
            tables.merge_candidates: [],
        }
        self.mentions: list[dict[str, Any]] = []
        self.ledger: list[dict[str, Any]] = []


class _FakeWriter:
    project = "p1"
    build_id = uuid.uuid4()

    def __init__(self, store: _FakeStore) -> None:
        self._s = store

    async def fetch_all(self, table: Any, *where: Any) -> list[SimpleNamespace]:
        rows = self._s.rows[table]
        for predicate in where:
            rows = [r for r in rows if _matches(r, predicate)]
        return [SimpleNamespace(**r) for r in rows]

    async def insert(self, table: Any, /, **values: Any) -> None:
        self._s.rows[table].append(values)

    async def update(self, table: Any, row_id: Any, /, **values: Any) -> None:
        row = next(r for r in self._s.rows[table] if r["id"] == row_id)
        row.update(values)

    async def delete(self, table: Any, row_id: Any, /) -> None:
        self._s.rows[table] = [r for r in self._s.rows[table] if r["id"] != row_id]

    async def repoint_mentions(self, from_entity: Any, to_entity: Any) -> int:
        moved = 0
        for mention in self._s.mentions:
            if mention["entity_id"] == from_entity:
                mention["entity_id"] = to_entity
                moved += 1
        return moved


class _FakeConn:
    def __init__(self, store: _FakeStore) -> None:
        self._s = store

    async def execute(self, statement: Any) -> Any:
        if isinstance(statement, sa.Insert):  # the auto-merge ledger record
            self._s.ledger.append(dict(statement.compile().params))
            return SimpleNamespace(rowcount=1)
        sql = str(statement)
        if "review_ledger" in sql:
            ledger_rows = [SimpleNamespace(**row) for row in self._s.ledger]
            return SimpleNamespace(fetchall=lambda: ledger_rows)
        # the mention-count group-by is iterated directly
        counts: dict[Any, int] = {}
        for mention in self._s.mentions:
            counts[mention["entity_id"]] = counts.get(mention["entity_id"], 0) + 1
        return iter([SimpleNamespace(entity_id=k, n=v) for k, v in counts.items()])


def _seed(
    store: _FakeStore,
    name: str,
    *,
    etype: str = "Company",
    mentions: int = 1,
    disambiguator: str | None = None,
    status: str = "active",
    review_status: str = "unreviewed",
) -> Any:
    eid = uuid.uuid4()
    store.rows[tables.entities].append(
        {
            "id": eid,
            "type": etype,
            "canonical_name": name,
            "entity_key": entity_key(etype, name, disambiguator),
            # persisted alongside the hash since DR-011 (the v2 ledger keys
            # re-mint from it) — the fake row models the real schema
            "disambiguator": disambiguator,
            "status": status,
            "review_status": review_status,
            "created_at": datetime.now(tz=UTC),
            "attributes": {},
        }
    )
    for _ in range(mentions):
        store.mentions.append({"entity_id": eid})
    return eid


def _ledger_row(kind: str, key: str, decision: str) -> dict[str, Any]:
    return {
        "target_kind": kind,
        "target_key": key,
        "decision": decision,
        "decided_by": "curator-1",
        "decided_at": datetime.now(tz=UTC),
        "fingerprint_version": LEDGER_FINGERPRINT_VERSION,
    }


async def _run(store: _FakeStore, **config: Any) -> Any:
    return await resolve_build(
        cast(AsyncConnection, _FakeConn(store)),
        cast(BuildScopedWriter, _FakeWriter(store)),
        ResolutionConfig(**config),
    )


async def test_auto_merge_records_the_decision_and_converges() -> None:
    """High-similarity pair merges toward the busier entity, the decision is
    RECORDED (decided_by='auto'), and a second pass does nothing (§5)."""
    store = _FakeStore()
    keep = _seed(store, "Acme Corporation", mentions=3)
    lose = _seed(store, "Acme Corporatio", mentions=1)
    report = await _run(store)
    assert report.auto_merged == 1 and report.mentions_repointed == 1
    by_id = {r["id"]: r for r in store.rows[tables.entities]}
    assert by_id[lose]["status"] == "merged"
    assert by_id[lose]["attributes"]["merged_into"] == str(keep)
    assert len(store.ledger) == 1 and store.ledger[0]["decided_by"] == "auto"
    assert all(m["entity_id"] == keep for m in store.mentions)

    second = await _run(store)
    assert second.auto_merged == 0 and second.ledger_merged == 0


async def test_ledger_precedence_governs_pairs() -> None:
    """A manual reject suppresses even a perfect-score pair; flipping it to
    merge fires the carry path (score-independent, and NOT re-recorded)."""
    store = _FakeStore()
    _seed(store, "Acme Corporation")
    _seed(store, "Acme Corporatio")
    key = ledger_merge_key(
        ledger_entity_key("Acme Corporation"), ledger_entity_key("Acme Corporatio")
    )
    store.ledger.append(_ledger_row("merge", key, "reject"))
    report = await _run(store)
    assert report.pairs_suppressed == 1
    assert report.auto_merged == 0 and report.candidates_created == 0

    store.ledger[0]["decision"] = "merge"
    ledger_size = len(store.ledger)
    carried = await _run(store)
    assert carried.ledger_merged == 1 and carried.auto_merged == 0
    assert len(store.ledger) == ledger_size  # carried merges are not re-recorded


async def test_entity_verdicts_apply_before_scoring() -> None:
    """§17: reject excludes the entity from projection AND pairing; approve
    stamps review_status; carry_review=False ignores the ledger (🔧)."""
    store = _FakeStore()
    good = _seed(store, "Acme Corporation")
    bad = _seed(store, "Acme Corporatio")
    store.ledger.append(_ledger_row("entity", ledger_entity_key("Acme Corporatio"), "reject"))
    store.ledger.append(_ledger_row("entity", ledger_entity_key("Acme Corporation"), "approve"))
    report = await _run(store)
    assert report.entities_rejected == 1 and report.entities_approved == 1
    assert report.auto_merged == 0  # the rejected twin never paired
    by_id = {r["id"]: r for r in store.rows[tables.entities]}
    assert by_id[bad]["status"] == "rejected"
    assert by_id[good]["review_status"] == "approved"

    fresh = _FakeStore()
    _seed(fresh, "Acme Corporation", mentions=2)
    _seed(fresh, "Acme Corporatio")
    fresh.ledger.append(_ledger_row("entity", ledger_entity_key("Acme Corporatio"), "reject"))
    unled = await _run(fresh, carry_review=False)
    assert unled.entities_rejected == 0 and unled.auto_merged == 1


async def test_mid_band_creates_candidate_once_and_low_band_nothing() -> None:
    store = _FakeStore()
    _seed(store, "Acme Corporation", mentions=2)
    _seed(store, "Acme Corporation Ltd")
    report = await _run(store, auto_merge_threshold=0.99, review_threshold=0.5)
    assert report.candidates_created == 1 and report.auto_merged == 0
    row = store.rows[tables.merge_candidates][0]
    assert row["status"] == "pending" and 0.5 <= row["score"] < 0.99
    again = await _run(store, auto_merge_threshold=0.99, review_threshold=0.5)
    assert again.candidates_created == 0

    low = _FakeStore()
    _seed(low, "Acme Corporation")
    _seed(low, "Acme Industries")  # shares a token, scores ~0.45
    nothing = await _run(low, auto_merge_threshold=0.99, review_threshold=0.6)
    assert nothing.candidates_created == 0 and nothing.auto_merged == 0


async def test_merge_cascade_reminting_over_fakes() -> None:
    """The re-mint arithmetic without PG: the loser's edge re-points to the
    canonical, its signature recomputes from the canonical's key, its
    evidence re-hashes; a collision with an existing canonical edge demotes
    the duplicate and dedups identical provenance."""
    from core.resolve.fingerprints import evidence_hash, relation_signature

    store = _FakeStore()
    keep = _seed(store, "Acme Corporation", mentions=3)
    lose = _seed(store, "Acme Corporatio", mentions=1)
    alice = _seed(store, "Alice", etype="Person")
    keep_key = entity_key("Company", "Acme Corporation")
    lose_key = entity_key("Company", "Acme Corporatio")
    alice_key = entity_key("Person", "Alice")

    def _edge(dst: Any, dst_key: str, ref: str) -> Any:
        rid = uuid.uuid4()
        sig = relation_signature(alice_key, "WORKS_AT", dst_key)
        store.rows[tables.relations].append(
            {
                "id": rid,
                "src_entity_id": alice,
                "dst_entity_id": dst,
                "type": "WORKS_AT",
                "relation_signature": sig,
                "status": "active",
                "attributes": {},
            }
        )
        store.rows[tables.relation_evidence].append(
            {
                "id": uuid.uuid4(),
                "relation_id": rid,
                "evidence_ref": ref,
                "quote": None,
                "evidence_hash": evidence_hash(sig, ref, None),
            }
        )
        return rid

    survivor = _edge(keep, keep_key, "9:employees:7")
    duplicate = _edge(lose, lose_key, "9:employees:7")  # same provenance → dedup

    report = await _run(store)
    assert report.auto_merged == 1
    assert report.duplicate_edges_demoted == 1
    assert report.duplicate_evidence_deleted == 1
    by_id = {r["id"]: r for r in store.rows[tables.relations]}
    assert by_id[duplicate]["status"] == "merged"
    assert by_id[duplicate]["relation_signature"] is None
    assert by_id[duplicate]["attributes"]["merged_into"] == str(survivor)
    remaining = store.rows[tables.relation_evidence]
    assert len(remaining) == 1 and remaining[0]["relation_id"] == survivor


async def test_disambiguated_namesakes_never_auto_merge() -> None:
    """C3's disambiguator deliberately keeps two 'Alice' Person rows with
    different external ids DISTINCT (§27.3). Identical normalized names with
    different keys can only mean that distinction — a 1.0 score must not
    destroy it, and no candidate may re-spam review every build. Only an
    explicit ledger merge joins them."""
    store = _FakeStore()
    _seed(store, "Alice", etype="Person", disambiguator="hr-1")
    _seed(store, "Alice", etype="Person", disambiguator="hr-2")
    report = await _run(store)
    assert report.auto_merged == 0 and report.candidates_created == 0
    assert report.namesakes_skipped == 1
    assert store.ledger == []  # nothing recorded — nothing decided

    # an explicit human merge decision still governs
    key = ledger_merge_key(ledger_entity_key("Alice", "hr-1"), ledger_entity_key("Alice", "hr-2"))
    store.ledger.append(_ledger_row("merge", key, "merge"))
    carried = await _run(store)
    assert carried.ledger_merged == 1


async def test_cross_type_twins_reach_review_but_never_auto_merge() -> None:
    """DR-011's first half: the same name under two types (the 區域探索廳
    shape — extraction collapses per (type, name), so the twin is two rows)
    scores 1.0 yet must land in REVIEW, not auto-merge — a type disagreement
    is definitionally 中信心, and auto-joining would conflate namesakes
    (Apple the company vs the fruit). Revert-probe: restore the type bucket
    in _blocked_pairs and no candidate exists at all (the old silent split);
    drop the cross-type clamp and this auto-merges."""
    store = _FakeStore()
    _seed(store, "區域探索廳", etype="Exhibit", mentions=2)
    _seed(store, "區域探索廳", etype="Location")
    report = await _run(store)
    assert report.auto_merged == 0
    assert report.candidates_created == 1
    row = store.rows[tables.merge_candidates][0]
    assert row["status"] == "pending"
    assert {row["left_snapshot"]["type"], row["right_snapshot"]["type"]} == {
        "Exhibit",
        "Location",
    }
    # the candidate carries the TRUE score — not an epsilon-demoted one. A
    # mangled score both lies to the curator (1.0 is the signal「same name,
    # types disagree」) and, under review == auto (below), exits both bands.
    assert row["score"] == 1.0


async def test_cross_type_twins_survive_equal_thresholds() -> None:
    """review_threshold == auto_merge_threshold is a LEGAL config (both 1.0 =
    「only surface exact matches」— the validator allows equality and the
    strict-carry integration test runs it). The old epsilon-clamp demoted an
    exact cross-type score to just under auto == review, so the pair fell out
    of BOTH bands and silently vanished — dropping exactly the pairs DR-011
    exists to surface. Barred-from-auto must be structural (review_only), not
    score-mangling: the twin lands in review with its true 1.0 score."""
    store = _FakeStore()
    _seed(store, "區域探索廳", etype="Exhibit", mentions=2)
    _seed(store, "區域探索廳", etype="Location")
    report = await _run(store, auto_merge_threshold=1.0, review_threshold=1.0)
    assert report.auto_merged == 0
    assert report.candidates_created == 1
    assert store.rows[tables.merge_candidates][0]["score"] == 1.0


async def test_carried_merge_survives_type_drift() -> None:
    """DR-011's second half — the 白審 kill: a curator's merge is keyed by the
    TYPE-FREE v2 ledger key, so it still carries when the next build's LLM
    re-types both sides (全量實測: EXHIBIT→FACILITY/LOCATION drift re-keyed
    1/3 of prior decisions into dormancy under the type-bearing v1 keys).
    Revert-probe: key the ledger on the v1 merge_key again and the re-typed
    build sees no decision — the pair re-surfaces as a candidate."""
    key = ledger_merge_key(ledger_entity_key("區域探索廳"), ledger_entity_key("區域探索厅"))
    # build 1: both typed Exhibit; the human merge carries
    first = _FakeStore()
    _seed(first, "區域探索廳", etype="Exhibit", mentions=2)
    _seed(first, "區域探索厅", etype="Exhibit")
    first.ledger.append(_ledger_row("merge", key, "merge"))
    r1 = await _run(first)
    assert r1.ledger_merged == 1

    # build 2: the LLM re-typed BOTH sides — the SAME ledger row still governs
    drifted = _FakeStore()
    _seed(drifted, "區域探索廳", etype="Location", mentions=2)
    _seed(drifted, "區域探索厅", etype="Facility")
    drifted.ledger.append(_ledger_row("merge", key, "merge"))
    r2 = await _run(drifted)
    assert r2.ledger_merged == 1
    assert r2.candidates_created == 0  # carried, not re-surfaced (the 白審 fix)


async def test_both_disambiguated_pairs_cap_at_candidate() -> None:
    """Two entities that BOTH carry (necessarily different) external ids are
    asserted distinct by their sources — near-identical names may propose a
    candidate for human review but must never auto-merge."""
    store = _FakeStore()
    _seed(store, "Acme Corporation", disambiguator="reg-1", mentions=2)
    _seed(store, "Acme Corporatio", disambiguator="reg-2")
    report = await _run(store)  # score ~0.97 >= default auto threshold
    assert report.auto_merged == 0
    assert report.candidates_created == 1
    row = store.rows[tables.merge_candidates][0]
    assert row["status"] == "pending"

    # one-sided ids stay mergeable: ER's job is joining the id-less mention
    free = _FakeStore()
    _seed(free, "Acme Corporation", disambiguator="reg-1", mentions=2)
    _seed(free, "Acme Corporatio")  # no external id
    merged = await _run(free)
    assert merged.auto_merged == 1


async def test_manual_approve_resurrects_a_rejected_row() -> None:
    """§27.3 latest-manual-wins must work WITHIN a build: an earlier pass
    rejected the row (status residue), a curator approves — the row comes
    back active/approved and re-enters pairing; merged rows stay merged
    (undo is 'split', deferred)."""
    store = _FakeStore()
    bad = _seed(store, "Acme Corporatio", status="rejected", review_status="rejected")
    key = ledger_entity_key("Acme Corporatio")
    reject = _ledger_row("entity", key, "reject")
    reject["decided_at"] = datetime(2026, 7, 1, tzinfo=UTC)
    approve = _ledger_row("entity", key, "approve")
    approve["decided_at"] = datetime(2026, 7, 2, tzinfo=UTC)  # newer manual wins
    store.ledger.extend([reject, approve])
    report = await _run(store)
    assert report.entities_restored == 1 and report.entities_rejected == 0
    by_id = {r["id"]: r for r in store.rows[tables.entities]}
    assert by_id[bad]["status"] == "active"
    assert by_id[bad]["review_status"] == "approved"

    merged_store = _FakeStore()
    gone = _seed(merged_store, "Acme Corporatio", status="merged")
    merged_store.ledger.append(_ledger_row("entity", key, "approve"))
    untouched = await _run(merged_store)
    assert untouched.entities_restored == 0
    assert {r["id"]: r for r in merged_store.rows[tables.entities]}[gone]["status"] == "merged"


async def test_one_sided_exact_namesake_merges_toward_the_id_bearing_side() -> None:
    """Round 2's over-block dual: an id-less text 'Alice' has asserted
    NOTHING, so it must merge into the id-bearing structured 'Alice' (that
    is ER's job) — and the ID side survives as canonical even with fewer
    mentions, because its key is the stronger identity and the one future
    structured builds re-mint."""
    store = _FakeStore()
    with_id = _seed(store, "Alice", etype="Person", disambiguator="hr-1", mentions=1)
    _seed(store, "Alice", etype="Person", mentions=5)  # busier, but id-less
    report = await _run(store)
    assert report.auto_merged == 1 and report.namesakes_skipped == 0
    by_id = {r["id"]: r for r in store.rows[tables.entities]}
    assert by_id[with_id]["status"] == "active"  # the id side survived
    assert all(m["entity_id"] == with_id for m in store.mentions)


async def test_relation_ledger_reapplies_after_remint() -> None:
    """A curator rejected canonical->X's identity in an earlier build; this
    build extracted the relation against the pre-merge loser, so the initial
    ledger pass could not see it. The re-mint must re-apply the decision —
    otherwise the rejected identity sits ACTIVE in the projection. The
    verdict is keyed by the v2 LEDGER signature (DR-011, type-free), never
    the fpv1 storage signature the row itself re-mints."""
    from core.resolve.fingerprints import relation_signature

    store = _FakeStore()
    keep = _seed(store, "Acme Corporation", mentions=3)
    lose = _seed(store, "Acme Corporatio", mentions=1)
    x = _seed(store, "Alice", etype="Person")
    keep_key = entity_key("Company", "Acme Corporation")
    lose_key = entity_key("Company", "Acme Corporatio")
    alice_key = entity_key("Person", "Alice")
    pre_merge_sig = relation_signature(alice_key, "WORKS_AT", lose_key)
    post_merge_sig = relation_signature(alice_key, "WORKS_AT", keep_key)
    post_merge_ledger_sig = ledger_relation_signature(
        ledger_entity_key("Alice"), "WORKS_AT", ledger_entity_key("Acme Corporation")
    )
    rid = uuid.uuid4()
    store.rows[tables.relations].append(
        {
            "id": rid,
            "src_entity_id": x,
            "dst_entity_id": lose,
            "type": "WORKS_AT",
            "relation_signature": pre_merge_sig,
            "status": "active",
            "review_status": "unreviewed",
            "attributes": {},
        }
    )
    store.ledger.append(_ledger_row("relation", post_merge_ledger_sig, "reject"))

    report = await _run(store)
    assert report.auto_merged == 1 and report.relations_reminted == 1
    assert report.relations_rejected == 1  # the carried reject re-applied
    row = {r["id"]: r for r in store.rows[tables.relations]}[rid]
    assert row["relation_signature"] == post_merge_sig
    assert row["status"] == "rejected" and row["review_status"] == "rejected"
    assert keep  # canonical survived


async def test_rejected_relations_still_remint_and_approve_restores_them() -> None:
    """Identity is unconditional, status is not: a rejected edge touching the
    loser must still re-point/re-mint/re-hash (or a later approve would
    resurrect it aimed at a merged entity under its pre-merge signature) —
    landing in needs_review absent a new-signature verdict (§27.3: the old
    verdict keyed to a dead fingerprint neither carries nor sheds silently);
    an approve keyed to the NEW signature restores it, correctly aimed."""
    from core.resolve.fingerprints import evidence_hash, relation_signature

    def build_store() -> tuple[_FakeStore, Any, Any, str, str]:
        store = _FakeStore()
        keep = _seed(store, "Acme Corporation", mentions=3)
        lose = _seed(store, "Acme Corporatio", mentions=1)
        alice = _seed(store, "Alice", etype="Person")
        alice_key = entity_key("Person", "Alice")
        pre_sig = relation_signature(
            alice_key, "WORKS_AT", entity_key("Company", "Acme Corporatio")
        )
        post_sig = relation_signature(
            alice_key, "WORKS_AT", entity_key("Company", "Acme Corporation")
        )
        rid = uuid.uuid4()
        store.rows[tables.relations].append(
            {
                "id": rid,
                "src_entity_id": alice,
                "dst_entity_id": lose,
                "type": "WORKS_AT",
                "relation_signature": pre_sig,
                "status": "rejected",  # rejected BEFORE the merge
                "review_status": "rejected",
                "attributes": {},
            }
        )
        store.rows[tables.relation_evidence].append(
            {
                "id": uuid.uuid4(),
                "relation_id": rid,
                "evidence_ref": "9:employees:7",
                "quote": None,
                "evidence_hash": evidence_hash(pre_sig, "9:employees:7", None),
            }
        )
        return store, keep, rid, pre_sig, post_sig

    # no verdict on the new signature: re-minted and marked for RE-REVIEW —
    # the old rejection was keyed to a fingerprint that no longer names this
    # row (§27.3); it neither carries nor sheds silently, and the row stays
    # out of projection (only 'active' projects) while visibly pending (§17)
    store, keep, rid, _pre, post_sig = build_store()
    report = await _run(store)
    assert report.auto_merged == 1 and report.relations_reminted == 1
    assert report.relations_marked_rereview == 1
    row = {r["id"]: r for r in store.rows[tables.relations]}[rid]
    assert row["dst_entity_id"] == keep  # re-pointed at the canonical
    assert row["relation_signature"] == post_sig  # re-minted
    assert row["status"] == "needs_review"
    assert row["review_status"] == "unreviewed"
    ev = store.rows[tables.relation_evidence][0]
    assert ev["evidence_hash"] == evidence_hash(post_sig, "9:employees:7", None)

    # approve keyed to the NEW identity (its v2 LEDGER signature — the
    # storage sig is not a ledger key, DR-011): restored, correctly aimed
    store, keep, rid, _pre, post_sig = build_store()
    store.ledger.append(
        _ledger_row(
            "relation",
            ledger_relation_signature(
                ledger_entity_key("Alice"), "WORKS_AT", ledger_entity_key("Acme Corporation")
            ),
            "approve",
        )
    )
    report = await _run(store)
    assert report.relations_restored == 1
    row = {r["id"]: r for r in store.rows[tables.relations]}[rid]
    assert row["status"] == "active" and row["review_status"] == "approved"
    assert row["dst_entity_id"] == keep and row["relation_signature"] == post_sig


async def test_deferred_pairs_never_auto_merge_and_relist() -> None:
    """§17: defer 仍列入待審 — a curator's defer must hold a pair OPEN: no
    auto-merge however high the score (manual outranks auto), and the pair
    re-lists as a pending candidate so review can finish the job."""
    store = _FakeStore()
    _seed(store, "Acme Corporation", mentions=2)
    _seed(store, "Acme Corporatio")  # scores ~0.97, above the default auto bar
    key = ledger_merge_key(
        ledger_entity_key("Acme Corporation"), ledger_entity_key("Acme Corporatio")
    )
    store.ledger.append(_ledger_row("merge", key, "defer"))
    report = await _run(store)
    assert report.auto_merged == 0 and report.ledger_merged == 0
    assert report.candidates_created == 1
    assert store.rows[tables.merge_candidates][0]["status"] == "pending"
    # both entities still active — the graph did not change under a defer
    assert all(r["status"] == "active" for r in store.rows[tables.entities])


async def test_approved_old_signature_also_marks_rereview_on_remint() -> None:
    """The carry gap is symmetric: an APPROVE keyed to the pre-merge
    signature must not silently bless the re-minted identity either — same
    §27.3 rule, same needs_review outcome."""
    from core.resolve.fingerprints import relation_signature

    store = _FakeStore()
    _seed(store, "Acme Corporation", mentions=3)
    lose = _seed(store, "Acme Corporatio", mentions=1)
    alice = _seed(store, "Alice", etype="Person")
    pre_sig = relation_signature(
        entity_key("Person", "Alice"), "WORKS_AT", entity_key("Company", "Acme Corporatio")
    )
    rid = uuid.uuid4()
    store.rows[tables.relations].append(
        {
            "id": rid,
            "src_entity_id": alice,
            "dst_entity_id": lose,
            "type": "WORKS_AT",
            "relation_signature": pre_sig,
            "status": "active",
            "review_status": "approved",  # blessed under the OLD identity
            "attributes": {},
        }
    )
    report = await _run(store)
    assert report.relations_marked_rereview == 1
    row = {r["id"]: r for r in store.rows[tables.relations]}[rid]
    assert row["status"] == "needs_review" and row["review_status"] == "unreviewed"


async def test_approve_restores_needs_review_rows_to_projectability() -> None:
    """needs_review is the re-mint parking state (round 4): approve on the
    CURRENT signature is exactly the review it was parked for — it must exit
    the row to active, not stamp review_status while status keeps it
    excluded from projection forever. Same rule for entities (§17 lists
    needs_review in both lifecycles)."""
    from core.resolve.fingerprints import relation_signature

    store = _FakeStore()
    a = _seed(store, "Acme Corporation", mentions=2)
    alice = _seed(store, "Alice", etype="Person")
    sig = relation_signature(
        entity_key("Person", "Alice"), "WORKS_AT", entity_key("Company", "Acme Corporation")
    )
    rid = uuid.uuid4()
    store.rows[tables.relations].append(
        {
            "id": rid,
            "src_entity_id": alice,
            "dst_entity_id": a,
            "type": "WORKS_AT",
            "relation_signature": sig,
            "status": "needs_review",  # parked by an earlier re-mint
            "review_status": "unreviewed",
            "attributes": {},
        }
    )
    store.ledger.append(
        _ledger_row(
            "relation",
            ledger_relation_signature(
                ledger_entity_key("Alice"), "WORKS_AT", ledger_entity_key("Acme Corporation")
            ),
            "approve",
        )
    )
    report = await _run(store)
    assert report.relations_restored == 1
    row = {r["id"]: r for r in store.rows[tables.relations]}[rid]
    assert row["status"] == "active" and row["review_status"] == "approved"

    # entity variant: a needs_review entity approved via its entity_key
    estore = _FakeStore()
    parked = _seed(estore, "Globex", status="needs_review")
    estore.ledger.append(_ledger_row("entity", ledger_entity_key("Globex"), "approve"))
    ereport = await _run(estore)
    assert ereport.entities_restored == 1
    erow = {r["id"]: r for r in estore.rows[tables.entities]}[parked]
    assert erow["status"] == "active" and erow["review_status"] == "approved"
