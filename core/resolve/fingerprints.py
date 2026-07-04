"""Stable review fingerprints — the cross-build identity keys (DESIGN §27.3, DR-003/DR-007).

Review decisions survive rebuilds because they are keyed by fingerprints of
*content*, not by build-scoped row ids. These functions ARE the frozen spec:

- ``entity_key      = fpv{N}( norm(type) | norm(canonical_name) | disambiguator )``
- ``relation_signature = fpv{N}( src_entity_key | norm(type) | dst_entity_key )``
- ``merge_key       = fpv{N}( sorted(left_key, right_key) )``
- ``evidence_hash   = sha256( relation_signature | evidence_ref | norm(quote) )`` (§27.4)

``FINGERPRINT_VERSION`` (DR-007) is baked into every KEY as an ``fpv{N}:``
prefix. Any change to the normalization or composition rules below MUST bump
it — the ledger only applies same-version keys, so a silent change would
mis-apply past decisions. (``evidence_hash`` is NOT a ledger key and is not
prefixed — see :func:`evidence_hash`.)

The ``|`` in each formula is delimited concatenation realized as
length-prefixed encoding (:func:`_join`), NOT a literal ``|`` byte: a ``|``
*inside* a part must not collide two different part tuples into one digest
(``("a|b", "c")`` vs ``("a", "b|c")``). Every function here reads the spec's
``|`` that way — the convention `entity_key` established.
"""

from __future__ import annotations

import hashlib
import unicodedata

FINGERPRINT_VERSION = 1


def _norm(text: str) -> str:
    """Frozen normalization: NFKC → casefold → collapse internal whitespace."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


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
    (sorted pair), so (a, b) and (b, a) name the same decision."""
    first, second = sorted((left_key, right_key))
    return _fpv(first, second)


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
