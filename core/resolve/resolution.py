"""Entity resolution: fuzzy merge + review carry-forward (DESIGN §7/§17, C4).

Extraction (C3) already collapsed EXACT identities — two mentions of the same
normalized (type, name) are one entity row. This step handles what exact keys
cannot: ``Acme Corp`` vs ``ACME Corporation``. Per §7:

``blocking(type+正規化名) → similarity(字串+embedding 加權) → 高信心自動合併 /
中信心產 merge_candidate / 低信心不合併``

- **Blocking**: only same-normalized-type pairs that share a name token or a
  4-char prefix are scored (§7 names type+normalized-name as the block key;
  exact-equal names can't occur here — extraction already collapsed them).
- **Similarity**: normalized-name string ratio (stdlib ``SequenceMatcher``),
  weighted ``1 - embedding_weight``. 🔧 ``embedding_weight`` defaults 0.0:
  the pipeline computes embeddings at the INDEX step (§5 step 5, C5), which
  runs after resolve, so the embedding component becomes available when C5
  wires vectors in — the weight is the seam, not a stub.
- **Thresholds** 🟡 ``auto_merge_threshold``/``review_threshold``: score ≥
  auto ⇒ merge now (and RECORD it — decision='merge', decided_by='auto' in
  the ledger, so it carries forward per DR-003 and a curator can outrank it);
  review ≤ score < auto ⇒ a pending ``merge_candidates`` row; below ⇒ nothing.

**Ledger first (§17, 🔧 resolution.carry_review)**: before any scoring, the
project's ledger is applied — ``reject`` on an entity_key/relation_signature
excludes the row from projection (status='rejected'; C5 filters on status);
``approve`` marks review_status; a ``merge``/``approve`` on a merge_key
merges that pair regardless of score; ``reject`` on a merge_key suppresses
the pair (never re-proposed); ``defer`` re-lists a pending candidate.
Precedence is :func:`core.resolve.review.effective_decision` (manual outranks
auto, DR-007 same-version only).

**Merge application** — the C3a-flagged coupling, handled re-entrantly:
merging ``loser`` into ``canonical`` re-points mentions, then every relation
touching the loser gets its endpoint re-pointed and its ``relation_signature``
RE-MINTED from the canonical's entity_key (§27.3). A re-mint can collide with
an existing edge (the graph already had canonical→X): the loser's evidence
moves to the surviving edge with a RE-HASHED ``evidence_hash`` (§27.4 embeds
the signature); a re-hash that collides with stored evidence is a true
duplicate and is deleted (its twin carries identical provenance). The demoted
duplicate edge keeps its row for audit — status='merged', signature freed to
NULL (the partial unique index ignores NULLs) with the former signature and
winner recorded in ``attributes``.

Canonical selection is deterministic: more mentions → earlier created_at →
smaller id. Re-running resolve converges (§5): merged/rejected rows are
skipped, already-minted signatures match recomputation, and auto decisions
re-apply from the ledger instead of re-deciding.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from core.resolve import fingerprints
from core.resolve.review import AUTO_DECIDER, LedgerEntry, effective_decision
from core.stores import tables
from core.stores.repo import BuildScopedWriter


@dataclass(frozen=True)
class ResolutionConfig:
    """🟡 thresholds + 🔧 tunables (§7/§17/§23)."""

    auto_merge_threshold: float = 0.92  # 🟡 score ≥ this ⇒ merge without review
    review_threshold: float = 0.75  # 🟡 score ≥ this ⇒ merge_candidate
    embedding_weight: float = 0.0  # 🔧 §7 加權 — becomes live when C5 wires vectors
    carry_review: bool = True  # 🔧 resolution.carry_review (§17)

    def __post_init__(self) -> None:
        if not 0.0 <= self.review_threshold <= self.auto_merge_threshold <= 1.0:
            raise ValueError(
                "thresholds must satisfy 0 <= review_threshold <= "
                f"auto_merge_threshold <= 1 (got review={self.review_threshold}, "
                f"auto={self.auto_merge_threshold})"
            )
        if not 0.0 <= self.embedding_weight < 1.0:
            raise ValueError(
                f"embedding_weight must be in [0, 1) — 1.0 would zero the string "
                f"score with no vectors wired yet (got {self.embedding_weight})"
            )


@dataclass(frozen=True)
class ResolveReport:
    """What one resolve pass did (counts are THIS run's actions only)."""

    entities_rejected: int
    entities_approved: int
    relations_rejected: int
    relations_approved: int
    auto_merged: int
    ledger_merged: int
    candidates_created: int
    pairs_suppressed: int
    mentions_repointed: int
    relations_reminted: int
    duplicate_edges_demoted: int
    duplicate_evidence_deleted: int


@dataclass
class _Entity:
    """The resolver's working view of one active entity row."""

    id: uuid.UUID
    type: str
    name: str
    entity_key: str
    created_at: datetime
    attributes: dict[str, object] = field(default_factory=dict)
    mention_count: int = 0
    norm_name: str = field(init=False)

    def __post_init__(self) -> None:
        self.norm_name = fingerprints.norm_text(self.name)


def _string_score(a: _Entity, b: _Entity) -> float:
    return SequenceMatcher(None, a.norm_name, b.norm_name).ratio()


def _blocked_pairs(entities: list[_Entity]) -> list[tuple[_Entity, _Entity]]:
    """§7 blocking: same normalized type AND (shared name token OR shared
    4-char prefix). Keeps scoring off the full O(n²) while never separating
    the pairs the block key names."""
    by_type: dict[str, list[_Entity]] = {}
    for entity in entities:
        by_type.setdefault(fingerprints.norm_text(entity.type), []).append(entity)
    pairs: list[tuple[_Entity, _Entity]] = []
    for group in by_type.values():
        for i, a in enumerate(group):
            a_tokens = set(a.norm_name.split())
            for b in group[i + 1 :]:
                if a_tokens & set(b.norm_name.split()) or (
                    a.norm_name[:4] and a.norm_name[:4] == b.norm_name[:4]
                ):
                    pairs.append((a, b))
    return pairs


async def _load_ledger(
    conn: AsyncConnection, project: str
) -> dict[tuple[str, str], list[LedgerEntry]]:
    """All ledger rows for the project, grouped by (target_kind, target_key).

    The ledger is deliberately non-build-scoped (DR-003), so this reads via
    the raw connection — the same transaction the writer runs in.
    """
    rows = (
        await conn.execute(
            sa.select(tables.review_ledger).where(tables.review_ledger.c.project == project)
        )
    ).fetchall()
    grouped: dict[tuple[str, str], list[LedgerEntry]] = {}
    for row in rows:
        grouped.setdefault((row.target_kind, row.target_key), []).append(
            LedgerEntry(
                decision=row.decision,
                decided_by=row.decided_by,
                decided_at=row.decided_at,
                fingerprint_version=row.fingerprint_version,
            )
        )
    return grouped


def _decision(
    ledger: dict[tuple[str, str], list[LedgerEntry]], kind: str, key: str
) -> LedgerEntry | None:
    entries = ledger.get((kind, key))
    return effective_decision(entries) if entries else None


async def resolve_build(
    conn: AsyncConnection,
    writer: BuildScopedWriter,
    config: ResolutionConfig | None = None,
) -> ResolveReport:
    """Run §7 resolution over the writer's build (ledger first, §17).

    ``conn`` must be the same connection/transaction the writer is bound to —
    it exists solely because the ledger is outside the build-scoped world.
    """
    config = config or ResolutionConfig()
    ledger = await _load_ledger(conn, writer.project) if config.carry_review else {}
    now = datetime.now(tz=UTC)
    counts: dict[str, int] = dict.fromkeys(
        (
            "entities_rejected",
            "entities_approved",
            "relations_rejected",
            "relations_approved",
            "auto_merged",
            "ledger_merged",
            "candidates_created",
            "pairs_suppressed",
            "mentions_repointed",
            "relations_reminted",
            "duplicate_edges_demoted",
            "duplicate_evidence_deleted",
        ),
        0,
    )

    # --- §17: apply entity/relation decisions before anything else ---------
    # deterministic PROCESSING order, not just deterministic outcomes: the
    # merged_into audit chains depend on the sequence pairs are applied in
    entity_rows = sorted(await writer.fetch_all(tables.entities), key=lambda r: r.entity_key)
    entities: list[_Entity] = []
    key_of: dict[uuid.UUID, str] = {}
    for row in entity_rows:
        key_of[row.id] = row.entity_key
        verdict = _decision(ledger, "entity", row.entity_key)
        if verdict is not None and row.status not in ("rejected", "merged"):
            if verdict.decision == "reject" and row.status != "rejected":
                await writer.update(
                    tables.entities,
                    row.id,
                    status="rejected",
                    review_status="rejected",
                    updated_at=now,
                )
                counts["entities_rejected"] += 1
                continue
            if verdict.decision == "approve" and row.review_status != "approved":
                await writer.update(
                    tables.entities, row.id, review_status="approved", updated_at=now
                )
                counts["entities_approved"] += 1
        if row.status == "active" and (verdict is None or verdict.decision != "reject"):
            entities.append(
                _Entity(
                    row.id,
                    row.type,
                    row.canonical_name,
                    row.entity_key,
                    row.created_at,
                    dict(row.attributes or {}),
                )
            )

    for row in await writer.fetch_all(tables.relations):
        if row.relation_signature is None:
            continue
        verdict = _decision(ledger, "relation", row.relation_signature)
        if verdict is None:
            continue
        if verdict.decision == "reject" and row.status != "rejected":
            await writer.update(
                tables.relations,
                row.id,
                status="rejected",
                review_status="rejected",
                updated_at=now,
            )
            counts["relations_rejected"] += 1
        elif verdict.decision == "approve" and row.review_status != "approved":
            await writer.update(tables.relations, row.id, review_status="approved", updated_at=now)
            counts["relations_approved"] += 1

    # mention counts drive canonical selection (deterministic)
    mention_rows = await conn.execute(
        sa.select(tables.entity_mentions.c.entity_id, sa.func.count().label("n"))
        .select_from(
            tables.entity_mentions.join(
                tables.entities, tables.entities.c.id == tables.entity_mentions.c.entity_id
            )
        )
        .where(
            tables.entities.c.project == writer.project,
            tables.entities.c.build_id == writer.build_id,
        )
        .group_by(tables.entity_mentions.c.entity_id)
    )
    mention_count = {row.entity_id: row.n for row in mention_rows}
    for entity in entities:
        entity.mention_count = mention_count.get(entity.id, 0)

    # --- §7: block, score, decide ------------------------------------------
    existing_pairs = {
        (min(row.left_entity_id, row.right_entity_id), max(row.left_entity_id, row.right_entity_id))
        for row in await writer.fetch_all(tables.merge_candidates)
    }
    merged_away: set[uuid.UUID] = set()
    scored: list[tuple[float, _Entity, _Entity]] = []
    for a, b in _blocked_pairs(entities):
        score = _string_score(a, b) * (1.0 - config.embedding_weight)
        scored.append((score, a, b))
    # deterministic order: best score first, then stable key pair
    scored.sort(key=lambda item: (-item[0], item[1].entity_key, item[2].entity_key))

    for score, a, b in scored:
        if a.id in merged_away or b.id in merged_away:
            continue  # an endpoint already merged this pass; pair is stale
        merge_key = fingerprints.merge_key(a.entity_key, b.entity_key)
        verdict = _decision(ledger, "merge", merge_key)
        if verdict is not None and verdict.decision not in ("merge", "approve", "defer"):
            # reject — or ANY other present verdict (a stray 'split', a future
            # vocabulary member): a human has spoken about this pair, so auto
            # must never outrank it (§27.3 precedence), whatever it said.
            counts["pairs_suppressed"] += 1
            continue
        carried = verdict is not None and verdict.decision in ("merge", "approve")
        if carried or score >= config.auto_merge_threshold:
            canonical, loser = _pick_canonical(a, b)
            await _apply_merge(writer, canonical, loser, key_of, counts, now)
            merged_away.add(loser.id)
            if carried:
                counts["ledger_merged"] += 1
            else:
                counts["auto_merged"] += 1
                await conn.execute(
                    tables.review_ledger.insert().values(
                        project=writer.project,
                        target_kind="merge",
                        target_key=merge_key,
                        fingerprint_version=fingerprints.FINGERPRINT_VERSION,
                        decision="merge",
                        decided_by=AUTO_DECIDER,
                        decided_at=now,
                        reason=f"auto-merge at score {score:.4f}",
                    )
                )
        elif score >= config.review_threshold or (
            verdict is not None and verdict.decision == "defer"
        ):
            pair = (min(a.id, b.id), max(a.id, b.id))
            if pair in existing_pairs:
                continue  # already pending/decided in this build — converge
            await writer.insert(
                tables.merge_candidates,
                id=uuid.uuid4(),
                left_entity_id=a.id,
                right_entity_id=b.id,
                score=score,
                features={
                    "string_score": _string_score(a, b),
                    "embedding_weight": config.embedding_weight,
                },
                status="pending",
                left_snapshot={"type": a.type, "name": a.name, "entity_key": a.entity_key},
                right_snapshot={"type": b.type, "name": b.name, "entity_key": b.entity_key},
                impact={
                    "left_mentions": a.mention_count,
                    "right_mentions": b.mention_count,
                },
            )
            existing_pairs.add(pair)
            counts["candidates_created"] += 1

    return ResolveReport(**counts)


def _pick_canonical(a: _Entity, b: _Entity) -> tuple[_Entity, _Entity]:
    """Deterministic §7 canonical choice: more mentions → earlier created_at
    → smaller id. Determinism is what lets a re-run converge on the same
    canonical instead of flip-flopping."""
    ranked = sorted((a, b), key=lambda e: (-e.mention_count, e.created_at, str(e.id)))
    return ranked[0], ranked[1]


async def _apply_merge(
    writer: BuildScopedWriter,
    canonical: _Entity,
    loser: _Entity,
    key_of: dict[uuid.UUID, str],
    counts: dict[str, int],
    now: datetime,
) -> None:
    """Merge ``loser`` into ``canonical`` with the full re-mint cascade."""
    counts["mentions_repointed"] += await writer.repoint_mentions(loser.id, canonical.id)
    await writer.update(
        tables.entities,
        loser.id,
        status="merged",
        # merge INTO the existing attributes — a whole-object replace would
        # drop extracted properties the moment extraction starts setting any
        attributes={**loser.attributes, "merged_into": str(canonical.id)},
        updated_at=now,
    )
    key_of[loser.id] = canonical.entity_key

    # every relation touching the loser: re-point + re-mint (§27.3/§27.4)
    signature_owner = {
        row.relation_signature: row.id
        for row in await writer.fetch_all(tables.relations)
        if row.relation_signature is not None
    }
    evidence_hashes = {
        row.evidence_hash for row in await writer.fetch_all(tables.relation_evidence)
    }
    touching = await writer.fetch_all(
        tables.relations,
        sa.or_(
            tables.relations.c.src_entity_id == loser.id,
            tables.relations.c.dst_entity_id == loser.id,
        ),
    )
    for relation in touching:
        if relation.status in ("rejected", "merged"):
            continue
        new_src = canonical.id if relation.src_entity_id == loser.id else relation.src_entity_id
        new_dst = canonical.id if relation.dst_entity_id == loser.id else relation.dst_entity_id
        new_signature = fingerprints.relation_signature(
            key_of[new_src], relation.type, key_of[new_dst]
        )
        survivor_id = signature_owner.get(new_signature)
        if survivor_id is not None and survivor_id != relation.id:
            # the canonical already has this edge — move evidence, demote dup
            await _move_evidence(
                writer, relation.id, survivor_id, new_signature, evidence_hashes, counts
            )
            await writer.update(
                tables.relations,
                relation.id,
                status="merged",
                relation_signature=None,
                attributes={
                    **dict(relation.attributes or {}),
                    "merged_into": str(survivor_id),
                    "former_signature": relation.relation_signature,
                },
                updated_at=now,
            )
            counts["duplicate_edges_demoted"] += 1
            continue
        await writer.update(
            tables.relations,
            relation.id,
            src_entity_id=new_src,
            dst_entity_id=new_dst,
            relation_signature=new_signature,
            updated_at=now,
        )
        if relation.relation_signature in signature_owner:
            del signature_owner[relation.relation_signature]
        signature_owner[new_signature] = relation.id
        counts["relations_reminted"] += 1
        await _rehash_evidence(writer, relation.id, new_signature, evidence_hashes, counts)


async def _rehash_evidence(
    writer: BuildScopedWriter,
    relation_id: uuid.UUID,
    new_signature: str,
    evidence_hashes: set[str],
    counts: dict[str, int],
) -> None:
    """§27.4: evidence_hash embeds the signature — re-mint means re-hash."""
    rows = await writer.fetch_all(
        tables.relation_evidence, tables.relation_evidence.c.relation_id == relation_id
    )
    for row in rows:
        new_hash = fingerprints.evidence_hash(new_signature, row.evidence_ref, row.quote)
        if new_hash == row.evidence_hash:
            continue
        if new_hash in evidence_hashes:
            # a stored twin already carries this exact provenance (§27.4 dedup)
            await writer.delete(tables.relation_evidence, row.id)
            evidence_hashes.discard(row.evidence_hash)
            counts["duplicate_evidence_deleted"] += 1
            continue
        await writer.update(tables.relation_evidence, row.id, evidence_hash=new_hash)
        evidence_hashes.discard(row.evidence_hash)
        evidence_hashes.add(new_hash)


async def _move_evidence(
    writer: BuildScopedWriter,
    from_relation: uuid.UUID,
    to_relation: uuid.UUID,
    new_signature: str,
    evidence_hashes: set[str],
    counts: dict[str, int],
) -> None:
    """Move a demoted duplicate edge's evidence onto the surviving edge."""
    rows = await writer.fetch_all(
        tables.relation_evidence, tables.relation_evidence.c.relation_id == from_relation
    )
    for row in rows:
        new_hash = fingerprints.evidence_hash(new_signature, row.evidence_ref, row.quote)
        if new_hash in evidence_hashes and new_hash != row.evidence_hash:
            await writer.delete(tables.relation_evidence, row.id)
            evidence_hashes.discard(row.evidence_hash)
            counts["duplicate_evidence_deleted"] += 1
            continue
        await writer.update(
            tables.relation_evidence, row.id, relation_id=to_relation, evidence_hash=new_hash
        )
        evidence_hashes.discard(row.evidence_hash)
        evidence_hashes.add(new_hash)
