"""Why: core.query.results is the typed mirror of the FROZEN §16 wire contract
(mcp_response.schema.json). If the mirror can serialize something the schema
rejects — or rejects something the schema allows — every retrieval tool built
on it ships a contract violation. So these tests pin the mirror against the
real schema (the same validator test_contracts.py uses), plus the two rules
this layer OWNS rather than the schema: require_sources at construction, and
the score-desc / id-asc ordering that makes a response reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from core.query.results import (
    McpResponse,
    QueryWarning,
    RetrievalResult,
    SourceRef,
    ordered_results,
)

pytestmark = pytest.mark.contract

_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "contracts" / "mcp_response.schema.json").read_text(
        encoding="utf-8"
    )
)
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)

_BUILD = "7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d"
_CHUNK = "3d7e4a5b-6c73-4d84-be95-fa6b07c18d29"
_ENTITY = "2c6d3f4a-5b62-4c73-ad84-e95fa6b07c18"


def _chunk_result(score: float = 0.9, rid: str = _CHUNK) -> RetrievalResult:
    return RetrievalResult(
        result_type="chunk",
        id=rid,
        score=score,
        text="People Ops owns onboarding.",
        source_refs=(
            SourceRef(
                source_type="chunk",
                id=rid,
                source_uri="s3://acme/onboarding.md",
                metadata={"start_offset": 10, "end_offset": 40},
            ),
        ),
    )


def _entity_result(score: float = 0.8, rid: str = _ENTITY) -> RetrievalResult:
    return RetrievalResult(
        result_type="entity",
        id=rid,
        score=score,
        title="People Ops",
        source_refs=(SourceRef(source_type="chunk", id="chunk:h1:0"),),
    )


def _response(*results: RetrievalResult, warnings: tuple[QueryWarning, ...] = ()) -> McpResponse:
    return McpResponse(
        query="who owns onboarding?",
        tool="semantic_search",
        project="acme",
        build_id=_BUILD,
        results=ordered_results(results),
        warnings=warnings,
    )


def test_serialized_response_validates_against_the_frozen_schema() -> None:
    """A chunk result (uri + offsets) and an entity result (mention ref) each
    meeting its §27.2 minimum must produce a payload the frozen validator
    accepts — otherwise the mirror is stricter or looser than the contract."""
    payload = _response(
        _chunk_result(),
        _entity_result(),
        warnings=(QueryWarning("PARTIAL_RESULTS", "1 hit omitted"),),
    ).to_dict()
    _VALIDATOR.validate(payload)
    # the envelope names the build it read (DR-001) and semantic has no router
    assert payload["build_id"] == _BUILD
    assert payload["graph_context"] is None and payload["debug"] is None
    assert payload["schema_version"] == "1.0"


def test_require_sources_is_enforced_at_construction() -> None:
    """require_sources (§16/§27.2) is an INVARIANT of a result, not a hope: a
    result with no source_ref cannot be built, so an untraceable answer can
    never reach serialization in the first place."""
    with pytest.raises(ValueError, match="require_sources"):
        RetrievalResult(result_type="chunk", id="c1", score=0.5, source_refs=())


def test_source_ref_omits_empty_optional_fields() -> None:
    """A mention-only entity ref serializes to {source_type, id} — no null
    source_uri, no empty metadata — while a chunk ref keeps uri + offsets. The
    schema allows null source_uri, but emitting it on every mention ref would
    be noise the entity minimum never asks for."""
    bare = SourceRef(source_type="chunk", id="chunk:h1:0").to_dict()
    assert bare == {"source_type": "chunk", "id": "chunk:h1:0"}
    rich = SourceRef(
        source_type="chunk", id=_CHUNK, source_uri="s3://x", metadata={"start_offset": 1}
    ).to_dict()
    assert rich["source_uri"] == "s3://x" and rich["metadata"] == {"start_offset": 1}


def test_confidence_is_omitted_when_absent_but_kept_when_present() -> None:
    """score is always present (it drives ordering); confidence is optional and
    nullable — semantic emits none, so it must be absent rather than a
    misleading 0."""
    assert "confidence" not in _chunk_result().to_dict()
    scored = RetrievalResult(
        result_type="entity",
        id="e1",
        score=0.4,
        confidence=0.9,
        source_refs=(SourceRef("chunk", "chunk:h1:0"),),
    )
    assert scored.to_dict()["confidence"] == 0.9


def test_ordering_is_score_desc_then_id_asc() -> None:
    """§16 ordering with a deterministic tie-break: equal scores (common for
    exact vector matches) must fall back to id asc, or the same query could
    rank them differently run to run."""
    a = _entity_result(score=0.5, rid="aaa")
    b = _entity_result(score=0.9, rid="bbb")
    c = _entity_result(score=0.5, rid="ccc")  # ties with a → id breaks it
    ordered = ordered_results([a, b, c])
    assert [r.id for r in ordered] == ["bbb", "aaa", "ccc"]
