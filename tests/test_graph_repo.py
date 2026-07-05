"""Why: DR-004 keeps every build's graph in ONE Neo4j database, so DR-006's
"never mix versions" depends entirely on this layer: every Cypher template
must filter every node AND relationship by the bound (project, build_id), and
no caller may ever hand this layer query text (a spliced fragment would be
the graph twin of the raw-SQL escapes that took C1b three review rounds —
here the splice point simply does not exist). These tests pin that structure
without a live server; execution is covered by the integration tests.
"""

from __future__ import annotations

import inspect
import re
import uuid
from typing import cast

import pytest
from neo4j import AsyncSession
from sqlalchemy.ext.asyncio import AsyncConnection

from core.stores import graph as graph_module
from core.stores.graph import (
    BuildScopedGraphProjector,
    BuildScopedGraphRepo,
    RelationEndpointsNotProjectedError,
)
from core.stores.repo import BuildNotWritableError

_BUILD = uuid.uuid4()


def _repo() -> BuildScopedGraphRepo:
    # unit tests never talk to Neo4j; the session is a placeholder and the
    # internal token is the documented test seam past factory validation
    return BuildScopedGraphRepo(
        cast(AsyncSession, object()), "p1", _BUILD, _token=graph_module._CONSTRUCTION_TOKEN
    )


def _projector() -> BuildScopedGraphProjector:
    return BuildScopedGraphProjector(
        cast(AsyncConnection, object()),
        cast(AsyncSession, object()),
        "p1",
        _BUILD,
        _token=graph_module._CONSTRUCTION_TOKEN,
    )


def _all_cypher_templates() -> dict[str, str]:
    """Scan the MODULE for Cypher, so a template added later cannot dodge the
    scope test by not being registered anywhere — the false-green lesson: a
    universal-sounding test must enumerate ALL instances. Keyed on the
    patterns the test actually asserts about (:Entity/:REL), not on clause
    keywords — a future CALL/UNWIND template touching those patterns must
    still pass through the per-pattern scope count."""
    return {
        name: value
        for name, value in vars(graph_module).items()
        if name.startswith("_")
        and not name.startswith("__")
        and isinstance(value, str)
        and (":Entity" in value or ":REL" in value)
    }


def test_every_cypher_template_filters_every_pattern_by_the_scope() -> None:
    """§4's projection rule is per-pattern, not per-query: a single ``:Entity``
    or ``:REL`` pattern missing the scope would let one query mix builds even
    though every other pattern is filtered. So the pin counts patterns, not
    just presence.

    Two relationship-scoping forms are legal: an inline property map on the
    pattern (``[:REL… {build_id: $build_id}]``), or — ONLY where Cypher forbids
    the map (``shortestPath`` rejects properties in its pattern, verified
    live) — a whole-path ``all(rel IN relationships(p) …)`` guard covering
    every hop."""
    templates = _all_cypher_templates()
    # keep the scan honest: it must actually find the module's templates
    assert len(templates) == 8, sorted(templates)
    inline_rel = re.compile(r":REL[^\]]*\{build_id: \$build_id")
    path_guard = "all(rel IN relationships(p) WHERE rel.build_id = $build_id)"
    for name, template in templates.items():
        assert "$build_id" in template, name
        # every Entity node pattern carries BOTH scope properties, adjacent
        assert template.count(":Entity") == template.count("project: $project"), name
        assert template.count(":Entity") == template.count("build_id: $build_id, project"), name
        # every relationship pattern is scoped: inline map, or the whole-path guard
        unscoped = template.count(":REL") - len(inline_rel.findall(template))
        assert unscoped == 0 or (unscoped > 0 and path_guard in template), name


def test_public_surface_accepts_no_query_text() -> None:
    """The graph twin of C1b's raw-SQL guard, solved one level earlier: there
    is NO parameter through which a caller could pass Cypher. The public
    surface is pinned by name, so adding e.g. a run(query) helper is a loud,
    reviewed decision — not a drive-by convenience."""
    reader_public = {name for name in dir(BuildScopedGraphRepo) if not name.startswith("_")}
    assert reader_public == {
        "project",
        "build_id",
        "for_active_build",
        "fetch_entities",
        "entity_count",
        "relation_count",
        "neighbors",
        "shortest_path",
        "edges_among",
    }
    projector_public = {name for name in dir(BuildScopedGraphProjector) if not name.startswith("_")}
    assert projector_public == reader_public | {
        "for_building_build",
        "project_entity",
        "project_relation",
    }
    # and no public callable takes anything that smells like query text
    for cls in (BuildScopedGraphRepo, BuildScopedGraphProjector):
        for name in (n for n in dir(cls) if not n.startswith("_")):
            member = inspect.getattr_static(cls, name)
            if isinstance(member, property):
                continue
            func = member.__func__ if isinstance(member, classmethod) else member
            params = set(inspect.signature(func).parameters)
            assert not params & {"query", "cypher", "template", "statement"}, (cls, name)


def test_consumers_cannot_reach_the_session_or_mutate_the_scope() -> None:
    """DR-006's fence, same as the Postgres repo: no session to escape
    through, no setters, no __dict__ to smuggle state into."""
    repo = _repo()
    with pytest.raises(AttributeError):
        _ = repo.session  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        repo.build_id = uuid.uuid4()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        repo.project = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        repo.escape_hatch = object()  # type: ignore[attr-defined]


def test_direct_construction_is_fenced_off() -> None:
    """Factories are the only sanctioned bindings — for_active_build resolves
    the scope from Postgres (DR-001), for_building_build validates it."""
    with pytest.raises(TypeError, match="for_active_build"):
        BuildScopedGraphRepo(cast(AsyncSession, object()), "p1", uuid.uuid4())


def test_active_bound_repos_cannot_project() -> None:
    """§27.1: the active build is an immutable live snapshot — the READ type
    has no projection methods, and the writer inherits (never overrides) the
    read factory whose return type is pinned to the read-only class."""
    assert not hasattr(_repo(), "project_entity")
    assert not hasattr(_repo(), "project_relation")
    assert hasattr(_projector(), "project_entity")
    assert "for_active_build" not in vars(BuildScopedGraphProjector)


def test_scope_params_use_the_projected_string_form() -> None:
    """Neo4j has no UUID type — nodes store build_id as a string, so the
    filter parameter must be the same representation or every scoped read
    silently matches nothing (a false-empty, not an error)."""
    params = _repo()._scope_params()
    assert params == {"build_id": str(_BUILD), "project": "p1"}
    assert isinstance(params["build_id"], str)


def test_relation_endpoint_error_is_typed_and_carries_the_scope() -> None:
    """C5 orchestration needs to distinguish 'endpoint not projected' cleanly
    — type plus fields, not string parsing (same bar as the Postgres repo's
    typed errors)."""
    err = RelationEndpointsNotProjectedError("p1", _BUILD, "src-1", "dst-2")
    assert (err.project, err.build_id, err.src, err.dst) == ("p1", _BUILD, "src-1", "dst-2")
    assert isinstance(err, LookupError)


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, object]]:
        return self._rows


class _FakeSession:
    """Captures (template, parameters) so the scope-injection contract can be
    pinned without a server; returns canned rows."""

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._rows = rows or []

    async def run(self, query: str, parameters: dict[str, object]) -> _FakeResult:
        self.calls.append((query, parameters))
        return _FakeResult(self._rows)


class _FakePGConn:
    """Answers the projector's status revalidation with a fixed status."""

    def __init__(self, status: str | None) -> None:
        self._status = status

    async def execute(self, statement: object) -> _FakePGConn:
        return self

    def scalar_one_or_none(self) -> str | None:
        return self._status


def _wired_repo(session: _FakeSession) -> BuildScopedGraphRepo:
    return BuildScopedGraphRepo(
        cast(AsyncSession, session), "p1", _BUILD, _token=graph_module._CONSTRUCTION_TOKEN
    )


def _wired_projector(
    session: _FakeSession, status: str | None = "building"
) -> BuildScopedGraphProjector:
    return BuildScopedGraphProjector(
        cast(AsyncConnection, _FakePGConn(status)),
        cast(AsyncSession, session),
        "p1",
        _BUILD,
        _token=graph_module._CONSTRUCTION_TOKEN,
    )


async def test_scope_parameters_always_win_over_caller_parameters() -> None:
    """The graph twin of 'predicates can only narrow': the scope is merged
    LAST into the driver parameters, so even a caller that names build_id or
    project cannot re-point a template outside the binding."""
    session = _FakeSession()
    repo = _wired_repo(session)
    await repo._run(graph_module._ENTITY_COUNT, {"build_id": "evil", "project": "evil", "extra": 1})
    (_, parameters), *_ = session.calls
    assert parameters["build_id"] == str(_BUILD)
    assert parameters["project"] == "p1"
    assert parameters["extra"] == 1


async def test_reads_send_the_scoped_templates_and_unwrap_rows() -> None:
    """Reads must run the module templates verbatim (the scope pins above are
    only meaningful if these exact strings reach the driver) and return the
    payload, not driver record wrappers."""
    session = _FakeSession(rows=[{"entity": {"canonical_id": "e-1"}}])
    assert await _wired_repo(session).fetch_entities("person") == [{"canonical_id": "e-1"}]
    counts = _FakeSession(rows=[{"total": 7}])
    repo = _wired_repo(counts)
    assert await repo.entity_count() == 7
    assert await repo.relation_count() == 7
    sent = [call[0] for call in session.calls + counts.calls]
    assert sent == [
        graph_module._FETCH_ENTITIES,
        graph_module._ENTITY_COUNT,
        graph_module._RELATION_COUNT,
    ]
    assert session.calls[0][1]["entity_type"] == "person"


async def test_every_projection_write_revalidates_the_building_status() -> None:
    """§27.1 per write, not per binding (the cross-store TOCTOU): a projector
    whose build stopped being `building` must refuse BEFORE touching Neo4j —
    the typed error carries the offending status, and no Cypher is sent."""
    session = _FakeSession()
    stale = _wired_projector(session, status="active")
    with pytest.raises(BuildNotWritableError) as excinfo:
        await stale.project_entity("e-1", "person", "resolved")
    assert excinfo.value.status == "active"
    assert session.calls == []  # refused before any graph write

    ok = _wired_projector(_FakeSession(rows=[{"linked": 1}]))
    await ok.project_entity("e-1", "person", "resolved")
    await ok.project_relation("e-1", "e-2", "works_at")


async def test_relation_projection_with_missing_endpoints_fails_loud() -> None:
    """A MERGE whose MATCHed endpoints don't exist writes nothing and reports
    nothing — the exact silent no-op that would hide C5 ordering bugs, so the
    projector turns 'zero rows linked' into the typed error."""
    no_rows: list[dict[str, object]] = []
    zero_linked: list[dict[str, object]] = [{"linked": 0}]
    for rows in (no_rows, zero_linked):
        projector = _wired_projector(_FakeSession(rows=rows))
        with pytest.raises(RelationEndpointsNotProjectedError):
            await projector.project_relation("e-1", "e-ghost", "works_at")


# -- C6c read surface (templates are fixed; hops is the one validated embed) ---


async def test_neighbors_substitutes_a_validated_hop_bound_and_a_deadline() -> None:
    """The hop bound cannot be a driver parameter (Cypher rejects `*1..$hops` —
    verified live), so it is embedded — but ONLY as a validated int, and the
    policy deadline rides the Query object so Neo4j kills a runaway traversal
    server-side (§21)."""
    session = _FakeSession()
    repo = _wired_repo(session)
    await repo.neighbors("seed-1", hops=3, limit=10, timeout_ms=1500)
    (query, parameters), *_ = session.calls
    text = str(query)
    assert "*1..3" in text and "__HOPS__" not in text  # substituted, validated
    assert getattr(query, "timeout", None) == 1.5  # the §21 deadline, in seconds
    assert parameters["seed"] == "seed-1" and parameters["limit"] == 10
    assert parameters["build_id"] == str(_BUILD)  # scope still merged last


@pytest.mark.parametrize("hops", [0, -1, True, 2.0, "2", "1 OR x"])
async def test_a_non_positive_or_non_int_hop_bound_is_refused(hops: object) -> None:
    """The embed seam accepts a positive int and NOTHING else — bool included
    (True would render as the string 'True' mid-pattern), so no value can
    smuggle pattern text into the one dynamic slot."""
    repo = _wired_repo(_FakeSession())
    with pytest.raises(ValueError, match="hops must be a positive int"):
        await repo.neighbors("s", hops=hops, limit=5, timeout_ms=1000)  # type: ignore[arg-type]


async def test_limit_and_timeout_are_validated_ints_too() -> None:
    session = _FakeSession()
    repo = _wired_repo(session)
    with pytest.raises(ValueError, match="limit must be a positive int"):
        await repo.neighbors("s", hops=1, limit=0, timeout_ms=1000)
    with pytest.raises(ValueError, match="timeout_ms must be a positive int"):
        await repo.neighbors("s", hops=1, limit=5, timeout_ms=0)
    assert session.calls == []  # refused before any query was sent


async def test_edges_among_short_circuits_an_empty_id_set() -> None:
    """No ids → no query at all (an `IN []` round-trip would be a wasted call
    under the phase deadline)."""
    session = _FakeSession()
    repo = _wired_repo(session)
    assert await repo.edges_among([], timeout_ms=1000) == []
    assert session.calls == []


async def test_shortest_path_returns_none_when_no_row_comes_back() -> None:
    session = _FakeSession(rows=[])
    repo = _wired_repo(session)
    assert await repo.shortest_path("a", "b", max_hops=3, timeout_ms=1000) is None
    (query, parameters), *_ = session.calls
    assert "*..3" in str(query) and parameters["src"] == "a" and parameters["dst"] == "b"
