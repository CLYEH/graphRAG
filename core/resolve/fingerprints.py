"""Stable review fingerprints — the cross-build identity keys (DESIGN §27.3, DR-003/DR-007).

Review decisions survive rebuilds because they are keyed by fingerprints of
*content*, not by build-scoped row ids. TWO key families live here (§27.3,
DR-011/GOV1), each versioned independently per DR-007:

**Storage keys (``fpv1``, FINGERPRINT_VERSION)** — per-build identity/dedup,
type-BEARING (two same-name entities of different types are distinct rows):

- ``entity_key      = fpv1( norm(type) | norm(canonical_name) | disambiguator )``
- ``relation_signature = fpv1( src_entity_key | norm(type) | dst_entity_key )``
- ``merge_key       = fpv1( sorted(left_key, right_key) )`` (legacy ledger key)
- ``proposal_key    = fpv1( norm(kind) | norm(type_name) )`` (§6 proposal pool)
- ``evidence_hash   = sha256( relation_signature | evidence_ref | norm(quote) )`` (§27.4)

**Ledger keys (``fpv2``, LEDGER_FINGERPRINT_VERSION)** — the cross-build
``review_ledger`` target_keys, type-FREE (DR-011): a human's approve/reject is
about the THING, while the LLM's type label is unstable evidence — 全量實測
same real entity splits across 4 types and type drift re-keyed 1/3 of prior
decisions into dormancy (白審). The entity component keys on
``(norm(canonical_name), disambiguator)`` only:

- ``ledger_entity_key        = fpv2( norm(canonical_name) | disambiguator )``
- ``ledger_relation_signature = fpv2( src_ledger_key | norm(type) | dst_ledger_key )``
- ``ledger_merge_key         = fpv2( sorted(left_ledger_key, right_ledger_key) )``

Accepted tradeoff (documented in DESIGN §27.3): entities distinguishable ONLY
by their LLM-assigned type alias in the ledger — by design (the label must not
partition decisions); sources that assert true namesakes do so via the
DISAMBIGUATOR, which stays in the key.

Any change to the normalization or composition rules of either family MUST
bump that family's version — the ledger only applies same-version keys, so a
silent change would mis-apply past decisions. (``evidence_hash`` is NOT a
ledger key and is not prefixed — see :func:`evidence_hash`.)

The ``|`` in each formula is delimited concatenation realized as
length-prefixed encoding (:func:`_join`), NOT a literal ``|`` byte: a ``|``
*inside* a part must not collide two different part tuples into one digest
(``("a|b", "c")`` vs ``("a", "b|c")``). Every function here reads the spec's
``|`` that way — the convention `entity_key` established.
"""

from __future__ import annotations

import hashlib
import unicodedata

#: The STORAGE key family's version (entity_key/relation_signature/
#: proposal_key prefixes) — per-build identity, formulas unchanged since v1.
FINGERPRINT_VERSION = 1

#: The LEDGER key family's version (review_ledger target_keys +
#: review_ledger.fingerprint_version). v2 = type-free entity component
#: (DR-011/GOV1); v1 ledger rows are dormant per DR-007 (標記重審,
#: never silently mis-applied).
LEDGER_FINGERPRINT_VERSION = 2


def _norm(text: str) -> str:
    """Frozen normalization: NFKC → casefold → collapse internal whitespace."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def norm_text(text: str) -> str:
    """The frozen normalization, publicly: resolution's blocking/scoring (§7)
    must group and compare by the SAME rule the identity keys are minted with
    — a second normalization implementation would be checker/consumer drift
    by construction."""
    return _norm(text)


def _join(*parts: str) -> str:
    """Length-prefix parts before joining so no character inside a part can
    collide two different part tuples (see module docstring)."""
    return "".join(f"{len(part)}:{part}" for part in parts)


def _fpv(*parts: str) -> str:
    """Hash parts into a versioned key (length-prefix-safe join)."""
    digest = hashlib.sha256(_join(*parts).encode("utf-8")).hexdigest()
    return f"fpv{FINGERPRINT_VERSION}:{digest}"


def entity_key(entity_type: str, canonical_name: str, disambiguator: str | None = None) -> str:
    """Cross-build identity of an entity. ``disambiguator`` is a stable
    external id when one exists (§27.3) — trimmed but NOT case-normalized,
    since external ids may be case-sensitive. Blank after trimming counts as
    absent: connectors represent "no id" as None, "" or whitespace
    interchangeably, and all three must mint the SAME key or carry-forward
    breaks across sources."""
    parts = [_norm(entity_type), _norm(canonical_name)]
    if disambiguator is not None:
        trimmed = disambiguator.strip()
        if trimmed:
            parts.append(trimmed)
    return _fpv(*parts)


def relation_signature(src_entity_key: str, relation_type: str, dst_entity_key: str) -> str:
    """Cross-build identity of a relation: directed src → dst."""
    return _fpv(src_entity_key, _norm(relation_type), dst_entity_key)


def merge_key(left_key: str, right_key: str) -> str:
    """Cross-build identity of a merge decision — symmetric by construction
    (sorted pair), so (a, b) and (b, a) name the same decision. LEGACY ledger
    key (v1, type-bearing via its entity_key inputs): new ledger rows key on
    :func:`ledger_merge_key`; this stays for the frozen v1 formula record."""
    first, second = sorted((left_key, right_key))
    return _fpv(first, second)


def _fpv_ledger(*parts: str) -> str:
    """Hash parts into a LEDGER-versioned key (length-prefix-safe join)."""
    digest = hashlib.sha256(_join(*parts).encode("utf-8")).hexdigest()
    return f"fpv{LEDGER_FINGERPRINT_VERSION}:{digest}"


def ledger_entity_key(canonical_name: str, disambiguator: str | None = None) -> str:
    """Cross-build REVIEW identity of an entity — type-FREE (v2, DR-011).

    Keys on ``(norm(canonical_name), disambiguator)`` only: the LLM's type
    label drifts across builds (the same real thing re-typed EXHIBIT →
    LOCATION), and a type-bearing review key turned every drift into 白審 —
    prior approve/reject rows keyed to the old type went dormant. The
    disambiguator (a stable EXTERNAL id, §27.3) stays in the key: it is how
    sources assert true namesakes, and those must never share a review
    identity. Same blank-folding as :func:`entity_key`."""
    parts = [_norm(canonical_name)]
    if disambiguator is not None:
        trimmed = disambiguator.strip()
        if trimmed:
            parts.append(trimmed)
    return _fpv_ledger(*parts)


def ledger_relation_signature(src_ledger_key: str, relation_type: str, dst_ledger_key: str) -> str:
    """Cross-build REVIEW identity of a relation (v2): directed src → dst over
    type-free endpoint keys — a relation decision survives its endpoints being
    re-typed, for the same reason :func:`ledger_entity_key` drops the type."""
    return _fpv_ledger(src_ledger_key, _norm(relation_type), dst_ledger_key)


def ledger_merge_key(left_ledger_key: str, right_ledger_key: str) -> str:
    """Cross-build REVIEW identity of a merge decision (v2) — symmetric
    (sorted pair) over type-free entity ledger keys, so「這兩個是同一個東西」
    survives either side being re-typed on the next build."""
    first, second = sorted((left_ledger_key, right_ledger_key))
    return _fpv_ledger(first, second)


def proposal_key(kind: str, type_name: str) -> str:
    """Cross-build identity of an ontology proposal (§6 待審池, C3c).

    ``fpv{N}( norm(kind) | norm(type_name) )`` — the pool dedups on it, so a
    later build re-proposing ``Spaceship`` (any casing/spacing) upserts into
    the existing row and a rejected type never re-opens review. ``kind`` is
    part of the identity: an entity type and a relation type may share a name
    without being one proposal.
    """
    return _fpv(_norm(kind), _norm(type_name))


def evidence_hash(relation_signature: str, evidence_ref: str, quote: str | None) -> str:
    """Dedup identity of one piece of relation evidence (§27.4).

    ``sha256( relation_signature | evidence_ref | norm(quote) )``. NOT
    ``fpv``-prefixed and NOT a ledger key: ``relation_signature`` already
    carries the fingerprint version, and DR-007 versions only
    entity_key/relation_signature/merge_key. ``quote`` is absent for row and
    manual evidence (§27.4: only chunk evidence has a span/quote) → normalized
    to ``""``, so every row/manual evidence of one (relation, ref) collapses
    to a single dedup key, which is the intended de-duplication.
    """
    digest = hashlib.sha256(
        _join(relation_signature, evidence_ref, _norm(quote or "")).encode("utf-8")
    ).hexdigest()
    return digest
