"""Stable review fingerprints — the cross-build identity keys (DESIGN §27.3, DR-003/DR-007).

Review decisions survive rebuilds because they are keyed by fingerprints of
*content*, not by build-scoped row ids. These functions ARE the frozen spec:

- ``entity_key      = fpv{N}( norm(type) | norm(canonical_name) | disambiguator )``
- ``relation_signature = fpv{N}( src_entity_key | norm(type) | dst_entity_key )``
- ``merge_key       = fpv{N}( sorted(left_key, right_key) )``

``FINGERPRINT_VERSION`` (DR-007) is baked into every key as an ``fpv{N}:``
prefix. Any change to the normalization or composition rules below MUST bump
it — the ledger only applies same-version keys, so a silent change would
mis-apply past decisions.
"""

from __future__ import annotations

import hashlib
import unicodedata

FINGERPRINT_VERSION = 1


def _norm(text: str) -> str:
    """Frozen normalization: NFKC → casefold → collapse internal whitespace."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _fpv(*parts: str) -> str:
    """Hash parts into a versioned key. Parts are length-prefixed before
    joining so no separator character inside a part can collide two different
    part tuples into one digest (e.g. ("a|b", "c") vs ("a", "b|c"))."""
    encoded = "".join(f"{len(part)}:{part}" for part in parts)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
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
