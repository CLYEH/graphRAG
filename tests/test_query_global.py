"""Why: global_summary serves the §8 global mode straight from the SoR —
what must hold is the emission discipline: rating-ranked deterministically
(pure function of the row set, any fetch order), every result cited by member
entity refs (§27.2), nullable display fields never crash or coerce, the top_k
ceiling clips with an EXACT TRUNCATED (judged over citable rows only, both
directions), and out-of-contract top_k degrades typed — never a 500. Every
response is validated against the frozen §16 schema.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
import pytest
import sqlalchemy as sa

from core.query.global_reports import global_summary
from core.query.results import McpResponse
from core.stores import tables
from core.stores.repo import BuildScopedRepo

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = json.loads((REPO_ROOT / "contracts" / "mcp_response.schema.json").read_text("utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(
    cast(dict[str, Any], _SCHEMA), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)

_PROJECT = "acme"
_BUILD = uuid.UUID("7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d")


class _FakeRepo:
    def __init__(self, rows: list[Any], known: set[Any] | None = None) -> None:
        self.project = _PROJECT
        self.build_id = _BUILD
        self._rows = rows
        # the build's entity ids (for member grounding); defaults to every id
        # the fixtures claim, so tests not about grounding stay focused
        self._known = (
            known
            if known is not None
            else {m for row in rows for m in (row.member_entity_ids or []) if m is not None}
        )

    async def fetch_all(self, table: sa.Table, *where: Any) -> list[Any]:
        if table is tables.entities:
            return [SimpleNamespace(id=entity_id) for entity_id in self._known]
        assert table is tables.community_reports
        return self._rows


def _report(
    rating: float | None,
    members: int = 2,
    title: str | None = "T",
    summary: str | None = "S",
    member_ids: list[Any] | None = None,
) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        level=0,
        title=title,
        summary=summary,
        rating=rating,
        member_entity_ids=(
            member_ids if member_ids is not None else [uuid.uuid4() for _ in range(members)]
        ),
    )


async def _run(rows: list[Any], top_k: int = 10, known: set[Any] | None = None) -> McpResponse:
    response = await global_summary(
        cast(BuildScopedRepo, _FakeRepo(rows, known)), "the question", top_k
    )
    _VALIDATOR.validate(response.to_dict())
    return response


def _codes(response: McpResponse) -> list[str]:
    return [w.code for w in response.warnings]


async def test_reports_rank_by_rating_desc_unrated_last_deterministically() -> None:
    """v1 ranking is the LLM's own importance signal: rating desc, unrated
    LAST, id tiebreak — a pure function of the row set (the #34 lesson: PG
    fetch order is not rerun-stable, so the emitted order must not depend on
    it)."""
    low, high, unrated = _report(2.0), _report(9.0), _report(None)
    forward = await _run([low, high, unrated])
    backward = await _run([unrated, high, low])  # a different fetch order
    ids = [r.id for r in forward.results]
    assert ids == [str(high.id), str(low.id), str(unrated.id)]  # rating desc, None last
    assert ids == [r.id for r in backward.results]  # order is fetch-order-proof
    assert forward.warnings == ()


async def test_every_result_cites_its_member_entities() -> None:
    """§27.2: community_report → member entity refs — EVERY member, as
    source_type=entity refs, sorted deterministically."""
    members = [uuid.uuid4() for _ in range(3)]
    response = await _run([_report(5.0, member_ids=members)])
    result = response.results[0]
    assert result.result_type == "community_report"
    assert [ref.source_type for ref in result.source_refs] == ["entity"] * 3
    assert [ref.id for ref in result.source_refs] == sorted(str(m) for m in members)
    assert result.title == "T" and result.text == "S"


async def test_nullable_display_fields_emit_null_not_a_crash() -> None:
    """title/summary are nullable in the SoR — a NULL (or blank) emits as
    null (§16 allows it); the citation, not the prose, is the contract."""
    response = await _run([_report(5.0, title=None, summary="   ")])
    result = response.results[0]
    assert result.title is None and result.text is None
    assert result.source_refs  # still fully cited


async def test_a_memberless_row_is_dropped_and_surfaced_never_uncitable() -> None:
    """The table CHECK forbids memberless rows, but emission re-checks rather
    than trusts (§27.2): a defensively-caught bad row drops with
    PARTIAL_RESULTS instead of crashing the non-empty-refs invariant."""
    good = _report(5.0)
    bad = _report(9.0, member_ids=[])
    response = await _run([good, bad])
    assert [r.id for r in response.results] == [str(good.id)]
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_truncation_is_exact_over_citable_rows_only() -> None:
    """TRUNCATED must be exact in BOTH directions (the C6c lesson): it fires
    when citable reports exceed top_k, and must NOT fire when the only rows
    past the ceiling are memberless (they could never be emitted at any
    ceiling)."""
    citable = [_report(float(i)) for i in range(3)]
    response = await _run(citable, top_k=2)
    assert len(response.results) == 2
    assert _codes(response) == ["TRUNCATED"]

    # exactly top_k citable + one memberless straggler → NOT truncated
    rows = [_report(9.0), _report(8.0), _report(1.0, member_ids=[])]
    response = await _run(rows, top_k=2)
    assert len(response.results) == 2
    assert _codes(response) == ["PARTIAL_RESULTS"]  # the drop, but no TRUNCATED


@pytest.mark.parametrize("bad", [0, -1, True, "3", 2.5])
async def test_an_out_of_contract_top_k_degrades_typed(bad: Any) -> None:
    """Out-of-contract input → typed GUARDRAIL_BLOCKED (§22), never a 500 —
    bool is not an int (it would silently mean top_k=1), a str would break
    comparisons downstream."""
    response = await _run([_report(5.0)], top_k=bad)
    assert response.results == () and _codes(response) == ["GUARDRAIL_BLOCKED"]


async def test_an_empty_build_yields_an_empty_result_without_warnings() -> None:
    response = await _run([])
    assert response.results == () and response.warnings == ()


async def test_equal_and_unrated_reports_still_order_fetch_order_proof() -> None:
    """The id TIEBREAK is load-bearing, not decorative: equal-rated (and
    multiple unrated) reports would otherwise fall back to Python's stable
    sort = fetch order = exactly the #34 non-rerun-stable bug. Proven to fail
    when the id tiebreak is removed from the sort key (revert-probe)."""
    tied_a, tied_b = _report(5.0), _report(5.0)
    unrated_a, unrated_b = _report(None), _report(None)
    rows = [tied_a, tied_b, unrated_a, unrated_b]
    forward = await _run(rows)
    backward = await _run(list(reversed(rows)))
    assert [r.id for r in forward.results] == [r.id for r in backward.results]


async def test_an_ungrounded_member_ref_is_dropped_not_emitted() -> None:
    """member_entity_ids is a bare uuid[] with NO foreign key — an id that is
    not an entity of THIS build (malformed or hand-written row) must never
    become an authoritative entity ref (it could point anywhere, including
    another build). The report survives on its grounded subset; the omission
    is surfaced."""
    real, bogus = uuid.uuid4(), uuid.uuid4()
    row = _report(5.0, member_ids=[real, bogus])
    response = await _run([row], known={real})
    result = response.results[0]
    assert [ref.id for ref in result.source_refs] == [str(real)]  # bogus never emitted
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_a_report_with_no_grounded_members_drops_entirely() -> None:
    """Zero grounded members = nothing citable — the whole report drops
    (§27.2), it is not emitted with fabricated refs."""
    good = _report(1.0)
    orphan = _report(9.0, member_ids=[uuid.uuid4()])
    response = await _run(
        [good, orphan],
        known={m for m in good.member_entity_ids},
    )
    assert [r.id for r in response.results] == [str(good.id)]
    assert _codes(response) == ["PARTIAL_RESULTS"]


async def test_grounding_lookups_are_batched_under_the_bind_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The IN predicate binds one parameter per id and PostgreSQL caps a
    statement at 32767 binds — a large build's collective member claims must
    split into batches, or global retrieval fails outright on big graphs.
    Every batch's hits union into the grounded set (nothing lost at seams)."""
    import core.query.global_reports as module

    monkeypatch.setattr(module, "_GROUNDING_BATCH", 2)
    members = [uuid.uuid4() for _ in range(5)]  # → 3 batches at size 2
    row = _report(5.0, member_ids=members)

    batches: list[list[Any]] = []

    class _BatchHonestRepo(_FakeRepo):
        async def fetch_all(self, table: sa.Table, *where: Any) -> list[Any]:
            if table is tables.entities:
                asked = list(where[0].right.value)  # the batch the IN actually names
                batches.append(asked)
                # honest fake: answer ONLY for the ids this batch asked about,
                # so the union-at-seams assertion below cannot be false-green
                return [SimpleNamespace(id=i) for i in asked if i in self._known]
            return await super().fetch_all(table, *where)

    response = await global_summary(
        cast(BuildScopedRepo, _BatchHonestRepo([row])), "the question", 10
    )
    _VALIDATOR.validate(response.to_dict())
    assert [len(b) for b in batches] == [2, 2, 1]  # ceil(5 / 2) batched queries
    assert len(response.results[0].source_refs) == 5  # all grounded across batches
    assert response.warnings == ()
