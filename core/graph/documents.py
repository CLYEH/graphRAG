"""Document extraction: schema-guided LLM → entities/relations (DESIGN §5 step 3, §6, C3b).

The LLM half of the graph step. Each text document's chunks (C2) are prompted
one at a time against the project's :class:`~core.graph.ontology.TextOntology`;
the model must answer strict JSON whose every relation carries a VERBATIM
quote — because §27.4 chunk evidence requires ``start/end offsets + quote +
source_uri``, and only a quote that literally occurs in the chunk can be
located to a span. That requirement is why extraction owns its prompt/parse
instead of a stock triplet extractor: triplets without spans cannot satisfy
the provenance contract.

The abstraction boundary is §3's: consumers hold a LlamaIndex ``LLM`` (OpenAI
+ Claude switchable via ``core.llm.factory``); this module never names a
provider.

Discipline mirrors C3a — identity is the frozen fingerprint, dedup makes the
pass idempotent (§5), and the two halves share :class:`BuildGraphState`, so a
rule-extracted "Acme" and an LLM-extracted "Acme" are ONE entity with mentions
from both sources. Failure containment per §22: an LLM error or malformed
answer marks that DOCUMENT ``failed`` (§18 item, stable ref = content_hash)
and the build continues. Everything the model offers but the pipeline cannot
accept is VISIBLE, never silent:

- an out-of-ontology entity type → a :class:`TypeProposal` in the report
  (the §6 待審池's input; storage lands with the proposal-pool slice);
- a relation whose quote is not found verbatim, whose type is unknown, or
  whose endpoint wasn't accepted → a :class:`Discarded` with the reason.

Evidence offsets are DOCUMENT-absolute (``chunk.start_offset + match``), the
same frame as chunk offsets (§27.4 spans point into the original text); the
stored quote is capped at the contract-frozen 512 chars (§27.4 存摘句) while
offsets keep the full span.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from llama_index.core.llms import LLM, ChatMessage

from core.graph.ontology import TextOntology
from core.graph.state import BuildGraphState
from core.ingest.connectors import TEXT_SUFFIXES
from core.observability.spec import ItemOutcome
from core.resolve import fingerprints
from core.stores import tables
from core.stores.repo import BuildScopedWriter

_CREATED_BY = "llm"

#: The text mimes C2's document connector produces — derived from the SAME
#: constant so the selector can't drift from the producer (checker/consumer).
_TEXT_MIMES = frozenset(TEXT_SUFFIXES.values())

#: §27.4: quote 上限 (contract-frozen value; the DB CHECK enforces it too).
_MAX_QUOTE_CHARS = 512

_SYSTEM_PROMPT = """\
You extract a knowledge graph from one text chunk.

Allowed entity types: {entity_types}
Allowed relation types: {relation_types}

Answer ONLY a JSON object, no prose, shaped exactly:
{{"entities": [{{"type": "...", "name": "...", "confidence": 0.0}}],
  "relations": [{{"src_type": "...", "src_name": "...", "type": "...",
                  "dst_type": "...", "dst_name": "...", "quote": "...",
                  "confidence": 0.0}}]}}

Rules: every relation's "quote" MUST be copied VERBATIM from the chunk (it is
the evidence span). Keep quotes short (under 300 characters). Only use the
allowed types; if the text clearly needs a type outside the list, still emit
the entity with that type — it will be routed to ontology review, not stored.
confidence is your 0..1 estimate."""


def chunk_source_ref(content_hash: str, ordinal: int) -> str:
    """The stable ref of one chunk: rebuild-stable (content_hash survives
    rebuilds, ordinal is deterministic — the chunker converges) and
    unambiguous (the hash is fixed-width hex, so the ``:`` cannot collide)."""
    return f"chunk:{content_hash}:{ordinal}"


@dataclass(frozen=True)
class TypeProposal:
    """An out-of-ontology type the LLM proposed (§6 待審池 input)."""

    kind: str  # "entity" | "relation"
    type_name: str
    example: str  # the name/quote it appeared with
    chunk_ref: str


@dataclass(frozen=True)
class Discarded:
    """One model offering the pipeline refused, and why (never silent)."""

    chunk_ref: str
    reason: str


@dataclass(frozen=True)
class TextExtractReport:
    """Step result: write counts, §18 outcomes, and everything held out."""

    entities: int
    relations: int
    mentions: int
    evidence: int
    outcomes: tuple[ItemOutcome, ...]
    proposals: tuple[TypeProposal, ...]
    discarded: tuple[Discarded, ...]


async def extract_documents(
    writer: BuildScopedWriter, llm: LLM, ontology: TextOntology
) -> TextExtractReport:
    """Extract entities/relations from this build's text documents via LLM.

    One LLM call per chunk. Idempotent like C3a: fingerprint dedup against
    shared state, so re-runs (and overlap with structured extraction) write
    nothing twice. A document whose ANY chunk fails (LLM error / non-JSON) is
    a ``failed`` item and later documents still run (§22); the §27.7 retry
    re-runs the whole document and converges on what already landed.
    """
    state = BuildGraphState()
    await state.preload(writer)
    counts = {"entities": 0, "relations": 0, "mentions": 0, "evidence": 0}
    outcomes: list[ItemOutcome] = []
    proposals: list[TypeProposal] = []
    discarded: list[Discarded] = []
    system = _SYSTEM_PROMPT.format(
        entity_types=", ".join(ontology.entity_types),
        relation_types=", ".join(ontology.relation_types),
    )

    for doc in await writer.fetch_all(tables.documents):
        if doc.mime not in _TEXT_MIMES:
            continue  # structured rows are C3a's; not this step's work items
        chunks = sorted(
            await writer.fetch_all(tables.chunks, tables.chunks.c.document_id == doc.id),
            key=lambda row: row.ordinal,
        )
        failed = False
        produced = False
        for chunk in chunks:
            ref = chunk_source_ref(doc.content_hash, chunk.ordinal)
            try:
                answer = await llm.achat(
                    [
                        ChatMessage(role="system", content=system),
                        ChatMessage(role="user", content=chunk.text),
                    ]
                )
                payload = _parse_answer(answer.message.content or "")
            except Exception:  # noqa: BLE001 — any LLM/parse failure = failed item (§22)
                failed = True
                break
            produced = (
                await _apply_chunk(
                    writer,
                    payload=payload,
                    ontology=ontology,
                    doc=doc,
                    chunk=chunk,
                    ref=ref,
                    state=state,
                    counts=counts,
                    proposals=proposals,
                    discarded=discarded,
                )
                or produced
            )
        status = "failed" if failed else ("extracted" if produced else "skipped")
        outcomes.append(ItemOutcome("document", doc.content_hash, status))

    return TextExtractReport(
        counts["entities"],
        counts["relations"],
        counts["mentions"],
        counts["evidence"],
        tuple(outcomes),
        tuple(proposals),
        tuple(discarded),
    )


def _parse_answer(text: str) -> dict[str, Any]:
    """Strictly parse the model's JSON object (fenced answers unwrapped).

    Shape is validated HERE, inside the caller's failure boundary, and the
    prompt's "shaped exactly" is enforced over the whole envelope value
    domain — absent field, wrong-typed field, wrong-typed items are ALL ways
    the same untrusted answer goes wrong: a non-list (``{"entities": 1}``)
    would raise outside the try and abort the pass; an ABSENT field (``{}``)
    would silently read as "found nothing" and record the document
    ``skipped``, hiding a schema-violating answer from observability and
    §27.7 retry-failed-only (which only re-runs ``failed``). A model that
    finds nothing must say so explicitly with empty lists.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body.removeprefix("json").strip()
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("model answer is not a JSON object")
    for field in ("entities", "relations"):
        if not isinstance(parsed.get(field), list):
            raise ValueError(f"model answer field {field!r} is missing or not a list")
    return parsed


def _strings(item: dict[str, Any], *fields: str) -> dict[str, str] | None:
    """The named fields as ACTUAL strings, or None if any isn't one.

    Leaf scalars are still untrusted model output: ``str()`` coercion would
    turn a non-string ({"text": "Alice"}, 42) into its Python repr and MINT
    it — a garbage canonical_name/fingerprint/quote persisted into the graph.
    Absent counts as non-string: identity/evidence fields have no default.
    """
    out: dict[str, str] = {}
    for field in fields:
        value = item.get(field)
        if not isinstance(value, str):
            return None
        out[field] = value
    return out


def _clamp(value: object) -> float:
    try:
        return min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


async def _apply_chunk(
    writer: BuildScopedWriter,
    *,
    payload: dict[str, Any],
    ontology: TextOntology,
    doc: Any,
    chunk: Any,
    ref: str,
    state: BuildGraphState,
    counts: dict[str, int],
    proposals: list[TypeProposal],
    discarded: list[Discarded],
) -> bool:
    """Persist one chunk's accepted extractions. Returns True if any landed."""
    now = datetime.now(tz=UTC)
    accepted_keys: dict[tuple[str, str], str] = {}  # (type, name) -> entity_key

    for item in payload["entities"]:
        if not isinstance(item, dict):
            discarded.append(Discarded(ref, f"entity item is not an object: {item!r}"))
            continue
        strings = _strings(item, "type", "name")
        if strings is None:
            discarded.append(Discarded(ref, f"entity type/name is not a string: {item!r}"))
            continue
        etype = strings["type"].strip()
        name = strings["name"].strip()
        if not etype or not name:
            discarded.append(Discarded(ref, f"entity missing type/name: {item!r}"))
            continue
        if etype not in ontology.entity_types:
            proposals.append(TypeProposal("entity", etype, name, ref))
            continue
        key = fingerprints.entity_key(etype, name)
        accepted_keys[(etype, name)] = key
        if key not in state.entity_id_by_key:
            entity_id = uuid.uuid4()
            await writer.insert(
                tables.entities,
                id=entity_id,
                type=etype,
                canonical_name=name,
                entity_key=key,
                attributes={},
                status="active",
                review_status="unreviewed",
                created_by=_CREATED_BY,
                created_at=now,
                updated_at=now,
            )
            state.entity_id_by_key[key] = entity_id
            counts["entities"] += 1
        entity_id = state.entity_id_by_key[key]
        if (entity_id, ref) not in state.mention_refs:
            await writer.insert_entity_mention(
                entity_id=entity_id,
                source_kind="text",
                source_ref=ref,
                surface_form=name,
                confidence=_clamp(item.get("confidence")),
            )
            state.mention_refs.add((entity_id, ref))
            counts["mentions"] += 1

    for item in payload["relations"]:
        if not isinstance(item, dict):
            discarded.append(Discarded(ref, f"relation item is not an object: {item!r}"))
            continue
        strings = _strings(item, "src_type", "src_name", "type", "dst_type", "dst_name", "quote")
        if strings is None:
            discarded.append(Discarded(ref, f"relation field is not a string: {item!r}"))
            continue
        rtype = strings["type"].strip()
        quote = strings["quote"]
        src = (strings["src_type"].strip(), strings["src_name"].strip())
        dst = (strings["dst_type"].strip(), strings["dst_name"].strip())
        if not rtype:
            discarded.append(Discarded(ref, f"relation missing type: {item!r}"))
            continue
        if rtype not in ontology.relation_types:
            proposals.append(TypeProposal("relation", rtype, quote or f"{src}→{dst}", ref))
            continue
        src_key = accepted_keys.get(src)
        dst_key = accepted_keys.get(dst)
        if src_key is None or dst_key is None:
            discarded.append(
                Discarded(ref, f"relation endpoint not among accepted entities: {src}→{dst}")
            )
            continue
        if not quote[:_MAX_QUOTE_CHARS].strip():
            # Blank is not evidence: a whitespace-only quote is truthy and
            # find(" ") matches almost any chunk, so it would mint an edge
            # whose stored quote satisfies the DB's quote <> '' CHECK yet
            # carries no auditable span. Checked on the 512-truncated prefix,
            # because THAT is what gets stored — a quote whose first 512
            # chars are all whitespace would store blank the same way.
            discarded.append(Discarded(ref, f"relation quote is blank: {quote[:80]!r}"))
            continue
        match = chunk.text.find(quote)
        if match < 0:
            # §27.4: chunk evidence MUST have a locatable span; a quote the
            # model paraphrased cannot be cited, and a relation without
            # evidence violates the §27.2 provenance minimum.
            discarded.append(Discarded(ref, f"quote not found verbatim in chunk: {quote[:80]!r}"))
            continue

        signature = fingerprints.relation_signature(src_key, rtype, dst_key)
        relation_id = state.relation_id_by_sig.get(signature)
        if relation_id is None:
            relation_id = uuid.uuid4()
            await writer.insert(
                tables.relations,
                id=relation_id,
                src_entity_id=state.entity_id_by_key[src_key],
                dst_entity_id=state.entity_id_by_key[dst_key],
                type=rtype,
                attributes={},
                relation_signature=signature,
                status="active",
                review_status="unreviewed",
                created_by=_CREATED_BY,
                confidence=_clamp(item.get("confidence")),
                created_at=now,
                updated_at=now,
            )
            state.relation_id_by_sig[signature] = relation_id
            counts["relations"] += 1

        stored_quote = quote[:_MAX_QUOTE_CHARS]
        digest = fingerprints.evidence_hash(signature, ref, stored_quote)
        if digest not in state.evidence_hashes:
            await writer.insert(
                tables.relation_evidence,
                id=uuid.uuid4(),
                relation_id=relation_id,
                evidence_type="chunk",
                evidence_ref=ref,
                chunk_id=chunk.id,
                start_offset=chunk.start_offset + match,
                end_offset=chunk.start_offset + match + len(quote),
                quote=stored_quote,
                source_uri=doc.source_uri,
                evidence_hash=digest,
                confidence=_clamp(item.get("confidence")),
                created_at=now,
            )
            state.evidence_hashes.add(digest)
            counts["evidence"] += 1

    return bool(accepted_keys)
