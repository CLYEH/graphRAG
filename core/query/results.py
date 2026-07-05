"""The §16 MCP retrieval response, as typed values (DESIGN §16/§27.2, DR-002).

``contracts/mcp_response.schema.json`` is the frozen wire contract every
retrieval tool shares (§16: 所有 retrieval 工具共用). This module is the typed
mirror the retrieval code (C6a semantic now; C6b–e as they land) builds, and
:meth:`McpResponse.to_dict` is the single serializer to that wire shape — so
one contract test validates the mirror against the schema and every tool
inherits it.

The models are deliberately GENERIC, not per-``result_type`` subclasses: the
schema keys everything off the ``result_type`` string and a uniform
``source_refs`` list, and the §27.2 per-type minimums are the *producer's*
responsibility (the semantic tool builds chunk/entity refs that meet them).
Encoding those minimums as separate classes here would duplicate the frozen
schema in Python and drift from it. The rules this layer DOES own are the two
that hold for every tool: ``require_sources`` (every result carries ≥1 ref)
and the ordering (score desc, ties broken by id asc) — both applied at
construction by :func:`ordered_results`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Bumped only on a breaking change (DR-002); mirrors the schema ``const``.
SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class SourceRef:
    """One traceable source behind a result (§27.2). ``source_uri``/``metadata``
    are omitted from the wire form when absent so a mention-only entity ref
    stays ``{source_type, id}`` while a chunk ref carries uri + offsets."""

    source_type: str  # document|chunk|entity|relation|row
    id: str
    source_uri: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source_type": self.source_type, "id": self.id}
        if self.source_uri is not None:
            out["source_uri"] = self.source_uri
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out


@dataclass(frozen=True)
class RetrievalResult:
    """One retrieval hit. ``source_refs`` is non-empty by contract
    (require_sources) — enforced at construction, not just hoped for."""

    result_type: str  # chunk|entity|relation|path|row|community_report
    id: str
    score: float
    source_refs: tuple[SourceRef, ...]
    title: str | None = None
    text: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.source_refs:
            raise ValueError(
                f"result {self.id!r} ({self.result_type}) has no source_refs — "
                "§16/§27.2 require_sources: an untraceable answer must not be emitted"
            )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "result_type": self.result_type,
            "id": self.id,
            "title": self.title,
            "text": self.text,
            "score": self.score,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
        }
        if self.confidence is not None:
            out["confidence"] = self.confidence
        return out


@dataclass(frozen=True)
class QueryWarning:
    """A typed degradation notice (§22) — the frozen §27.2 warning enum. Named
    ``QueryWarning`` (not ``Warning``) to avoid shadowing the builtin."""

    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class McpResponse:
    """A whole §16 response, bound to the active build (DR-001).

    ``graph_context`` and ``debug`` are pre-serialized (``Mapping``/``None``):
    the semantic tool has neither (single-mode, no router trace), so both are
    ``None``; graph-flavored tools and the C8 tool boundary (which reads
    ``query_policy.expose_debug`` and measures latency) fill them in.
    """

    query: str
    tool: str  # semantic_search|graph_query|global_summary|sql_query|hybrid_query
    project: str
    build_id: str
    results: tuple[RetrievalResult, ...]
    warnings: tuple[QueryWarning, ...] = ()
    graph_context: Mapping[str, Any] | None = None
    debug: Mapping[str, Any] | None = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "query": self.query,
            "tool": self.tool,
            "project": self.project,
            "build_id": self.build_id,
            "results": [result.to_dict() for result in self.results],
            "graph_context": dict(self.graph_context) if self.graph_context is not None else None,
            "warnings": [warning.to_dict() for warning in self.warnings],
            "debug": dict(self.debug) if self.debug is not None else None,
        }


def ordered_results(results: Sequence[RetrievalResult]) -> tuple[RetrievalResult, ...]:
    """§16 result ordering: score DESC, ties broken by id ASC.

    The tie-break is what makes a response REPRODUCIBLE — two hits with an
    identical score (common for exact vector matches) would otherwise come
    back in Qdrant's internal order, so the same query could rank them
    differently run to run. Applied once, here, so every tool inherits it.
    """
    return tuple(sorted(results, key=lambda r: (-r.score, r.id)))
