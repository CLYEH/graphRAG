"""Shared in-memory image of a build's graph for idempotent extraction (§5, C3).

Both extraction halves (structured C3a, documents C3b) write the same four
row kinds and dedup by the same frozen fingerprints; this state is the shared
fast path, seeded from what's already stored so a re-run — or the OTHER
half running second — reuses rows instead of duplicating them. Every key
matches a DB unique index, so the DB is the backstop and this is the cache
that also hands back the reusable row ids.
"""

from __future__ import annotations

import uuid

from core.stores import tables
from core.stores.repo import BuildScopedWriter


class BuildGraphState:
    """Fingerprint-keyed image of the entities/relations/evidence/mentions
    already in the build."""

    def __init__(self) -> None:
        self.entity_id_by_key: dict[str, uuid.UUID] = {}
        self.relation_id_by_sig: dict[str, uuid.UUID] = {}
        self.evidence_hashes: set[str] = set()
        self.mention_refs: set[tuple[uuid.UUID, str]] = set()

    async def preload(self, writer: BuildScopedWriter) -> None:
        for row in await writer.fetch_all(tables.entities):
            self.entity_id_by_key[row.entity_key] = row.id
        for row in await writer.fetch_all(tables.relations):
            if row.relation_signature is not None:
                self.relation_id_by_sig[row.relation_signature] = row.id
        for row in await writer.fetch_all(tables.relation_evidence):
            self.evidence_hashes.add(row.evidence_hash)
        self.mention_refs |= await writer.mention_refs()
