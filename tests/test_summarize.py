"""Why: summarize_build turns the SoR's active graph into community_reports —
the §8 global mode's data. What must hold: the partition is DETERMINISTIC (the
skip-idempotency identity is the member set, so a churning partition would
rewrite every rerun), reruns write nothing new (§5), one community's untrusted
LLM answer failing marks THAT item failed and the pass continues (§22/C3b),
and every written row can satisfy §27.2 (members present, sorted, citable).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import sqlalchemy as sa
from llama_index.core.llms import LLM

from core.stores import tables
from core.stores.repo import BuildScopedWriter
from core.summarize.communities import (
    _MIN_COMMUNITY_SIZE,
    _PROMPT_MEMBER_CAP,
    _community_ref,
    _leiden_communities,
    _parse_report,
    _prompt,
    summarize_build,
)


def _entity_row(entity_id: uuid.UUID, name: str = "E", status: str = "active") -> Any:
    return SimpleNamespace(id=entity_id, canonical_name=name, type="org", status=status)


def _relation_row(src: uuid.UUID, dst: uuid.UUID, rtype: str = "linked") -> Any:
    return SimpleNamespace(src_entity_id=src, dst_entity_id=dst, type=rtype)


class _FakeWriter:
    """Serves the three tables summarize_build reads and captures its inserts."""

    def __init__(
        self,
        entities: list[Any],
        relations: list[Any],
        reports: list[Any] | None = None,
    ) -> None:
        self._entities = entities
        self._relations = relations
        self._reports = reports or []
        self.inserted: list[dict[str, Any]] = []

    async def fetch_all(self, table: sa.Table, *where: Any) -> list[Any]:
        if table is tables.entities:
            # the caller filters status == 'active'; the fake honours it
            return [row for row in self._entities if row.status == "active"]
        if table is tables.relations:
            return self._relations
        if table is tables.community_reports:
            return self._reports
        raise AssertionError(f"unexpected table {table.name}")

    async def insert(self, table: sa.Table, /, **values: Any) -> None:
        assert table is tables.community_reports
        self.inserted.append(values)


class _FakeLLM:
    """Returns canned answers in sequence (or one fixed answer)."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        self.prompts.append(messages[-1].content)
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        return SimpleNamespace(message=SimpleNamespace(content=answer))


_GOOD = json.dumps({"title": "Acme cluster", "summary": "A tight-knit group.", "rating": 7})


async def _run(writer: _FakeWriter, llm: _FakeLLM) -> Any:
    return await summarize_build(cast(BuildScopedWriter, writer), cast(LLM, llm))


def _two_clusters() -> tuple[list[Any], list[Any], list[uuid.UUID]]:
    ids = sorted((uuid.uuid4() for _ in range(6)), key=str)
    entities = [_entity_row(eid, name=f"E{i}") for i, eid in enumerate(ids)]
    relations = [
        _relation_row(ids[0], ids[1]),
        _relation_row(ids[1], ids[2]),
        _relation_row(ids[0], ids[2]),
        _relation_row(ids[3], ids[4]),
        _relation_row(ids[4], ids[5]),
        _relation_row(ids[3], ids[5]),
    ]
    return entities, relations, ids


async def test_two_clusters_yield_two_written_reports() -> None:
    entities, relations, ids = _two_clusters()
    writer = _FakeWriter(entities, relations)
    report = await _run(writer, _FakeLLM(_GOOD))
    assert report.communities == 2 and report.written == 2
    assert [o.status for o in report.outcomes] == ["summarized", "summarized"]
    for row in writer.inserted:
        assert row["level"] == 0
        assert row["title"] == "Acme cluster" and row["summary"] == "A tight-knit group."
        assert row["rating"] == 7.0
        # §27.2: members present and deterministically sorted (the citation refs)
        assert row["member_entity_ids"] == sorted(row["member_entity_ids"], key=str)
        assert len(row["member_entity_ids"]) == 3


async def test_the_partition_is_deterministic_across_fetch_orders() -> None:
    """The rerun identity is the member set — a churning partition would
    defeat skip-idempotency. leidenalg is VERTEX-ORDER sensitive (relabeling
    the same graph can change the partition even with a fixed seed), and the
    entities arrive in Postgres fetch order, which is NOT stable across
    reruns — so the partition must be invariant under a PERMUTED input order.

    The graph here is deliberately MODULARITY-AMBIGUOUS (a 6-node ring plus
    one chord): on a symmetric graph like two disjoint triangles every vertex
    order yields the same forced partition, so such a test cannot fail when
    the sort is removed (that false-green shipped twice in review). This test
    was confirmed to FAIL when the sort line in _leiden_communities is
    reverted (reversed input then yields different member sets)."""
    ids = sorted((uuid.uuid4() for _ in range(6)), key=str)
    entities = [_entity_row(eid, name=f"E{i}") for i, eid in enumerate(ids)]
    ring_plus_chord = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0), (0, 3)]
    relations = [_relation_row(ids[a], ids[b]) for a, b in ring_plus_chord]
    first = await _run(_FakeWriter(entities, relations), _FakeLLM(_GOOD))
    shuffled = list(reversed(entities))  # a different fetch order, same graph
    second = await _run(_FakeWriter(shuffled, list(reversed(relations))), _FakeLLM(_GOOD))
    assert sorted(o.item_ref for o in first.outcomes) == sorted(o.item_ref for o in second.outcomes)


async def test_a_rerun_skips_communities_that_already_have_reports() -> None:
    """§5 rerun / C2 skip-idempotency: a community whose exact member set
    already has a report writes nothing new — the crash-retry cannot fan out
    duplicate reports."""
    entities, relations, ids = _two_clusters()
    writer = _FakeWriter(entities, relations)
    await _run(writer, _FakeLLM(_GOOD))
    rerun_writer = _FakeWriter(
        entities,
        relations,
        reports=[
            SimpleNamespace(member_entity_ids=row["member_entity_ids"]) for row in writer.inserted
        ],
    )
    rerun = await _run(rerun_writer, _FakeLLM(_GOOD))
    assert rerun.written == 0 and rerun_writer.inserted == []
    assert [o.status for o in rerun.outcomes] == ["skipped", "skipped"]


async def test_one_bad_llm_answer_fails_that_item_and_the_pass_continues() -> None:
    """§22: the failure boundary is the COMMUNITY — a bad answer marks that
    item failed (retryable, §27.7 stable ref) and the next community still
    runs; never an aborted pass, never a silently empty report."""
    entities, relations, _ = _two_clusters()
    writer = _FakeWriter(entities, relations)
    report = await _run(writer, _FakeLLM("not json at all", _GOOD))
    assert sorted(o.status for o in report.outcomes) == ["failed", "summarized"]
    assert report.written == 1 and len(writer.inserted) == 1


async def test_singletons_and_inactive_endpoints_never_reach_the_llm() -> None:
    """Isolated entities form singletons (below the size floor — no report),
    and a relation whose endpoint is non-active contributes no edge: the
    ACTIVE graph is what gets partitioned, not the raw relation table."""
    a, b, ghost, loner = (uuid.uuid4() for _ in range(4))
    entities = [
        _entity_row(a, "A"),
        _entity_row(b, "B"),
        _entity_row(ghost, "Ghost", status="rejected"),
        _entity_row(loner, "Loner"),
    ]
    relations = [
        _relation_row(a, b),
        _relation_row(a, ghost),  # inactive endpoint — no edge
    ]
    llm = _FakeLLM(_GOOD)
    writer = _FakeWriter(entities, relations)
    report = await _run(writer, llm)
    assert report.communities == 1 and report.written == 1  # only {A, B}
    assert len(llm.prompts) == 1
    members = writer.inserted[0]["member_entity_ids"]
    assert set(members) == {a, b}  # ghost and loner never surface


async def test_the_prompt_is_deterministic_across_relation_fetch_orders() -> None:
    """The relation sample the LLM sees is capped by SLICING — sliced unsorted,
    a retry whose fetch order shifted would sample different evidence and
    produce a non-reproducible report (the vertex-numbering lesson, on the
    prompt's sibling surface). The prompt must be byte-identical under a
    permuted relations input."""
    ids = sorted((uuid.uuid4() for _ in range(4)), key=str)
    entities = {eid: _entity_row(eid, name=f"E{i}") for i, eid in enumerate(ids)}
    relations = [
        _relation_row(ids[a], ids[b], rtype=f"r{a}{b}")
        for a in range(4)
        for b in range(4)
        if a != b
    ]
    forward = _prompt(ids, entities, relations)
    backward = _prompt(ids, entities, list(reversed(relations)))
    assert forward == backward  # byte-identical evidence sample


async def test_the_prompt_caps_members_but_counts_all() -> None:
    """A huge community must not blow the context window: the LLM sees a
    capped member sample, but member_count carries the real size (and the
    stored row still cites EVERY member)."""
    ids = sorted((uuid.uuid4() for _ in range(_PROMPT_MEMBER_CAP + 10)), key=str)
    entities = {eid: _entity_row(eid, name=f"E{i}") for i, eid in enumerate(ids)}
    payload = json.loads(_prompt(ids, entities, []))
    assert len(payload["members"]) == _PROMPT_MEMBER_CAP
    assert payload["member_count"] == len(ids)


def test_leiden_drops_singletons_but_keeps_size_two() -> None:
    a, b, loner = sorted((uuid.uuid4() for _ in range(3)), key=str)
    communities = _leiden_communities([a, b, loner], [_relation_row(a, b)])
    assert communities == [sorted([a, b], key=str)]
    assert _MIN_COMMUNITY_SIZE == 2  # the floor the docstring promises


def test_community_ref_is_order_insensitive_and_collision_safe() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _community_ref(sorted([a, b], key=str)) == _community_ref(sorted([b, a], key=str))
    assert _community_ref([a]) != _community_ref([b])


def test_parse_report_walks_the_whole_value_tree() -> None:
    """C3b: absent field, wrong type, blank string, bad rating are ALL the
    same failure (raise → item failed) — and a fenced answer still parses."""
    fenced = f"```json\n{_GOOD}\n```"
    assert _parse_report(fenced) == ("Acme cluster", "A tight-knit group.", 7.0)
    assert _parse_report(json.dumps({"title": "T", "summary": "S", "rating": None}))[2] is None
    for bad in [
        "not json",
        json.dumps(["a", "list"]),
        json.dumps({"summary": "S"}),  # absent title
        json.dumps({"title": "  ", "summary": "S"}),  # blank title
        json.dumps({"title": "T", "summary": ""}),  # blank summary
        json.dumps({"title": "T", "summary": "S", "rating": "high"}),  # wrong-typed rating
        json.dumps({"title": "T", "summary": "S", "rating": True}),  # bool is not a number
        json.dumps({"title": "T", "summary": "S", "rating": 11}),  # past the 0-10 contract
        json.dumps({"title": "T", "summary": "S", "rating": -1}),  # below it
        json.dumps({"title": 3, "summary": "S"}),  # wrong-typed title
    ]:
        try:
            _parse_report(bad)
        except (ValueError, json.JSONDecodeError):
            continue
        raise AssertionError(f"accepted bad report: {bad}")
