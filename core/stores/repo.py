"""Build-scoped repository over Postgres (DESIGN §27.1, DR-001/DR-006, C1b).

The structural guarantee DR-006 promises: query/MCP/api layers never touch a
raw store client, so they *cannot* forget a ``build_id`` filter and mix
versions. This module is that structure for Postgres:

- ``active_build_id`` — DR-001's single source of truth, one query against
  ``builds.status='active'`` (the partial unique index guarantees at most one
  row). No active build raises the typed ``NoActiveBuildError``, which the
  API layer maps to the frozen ``NO_ACTIVE_BUILD`` error code (§15).
- ``BuildScopedRepo`` — the READ capability: binds ``(project, build_id)``
  once at construction (§27.1: read the active id once per request and cache
  it — the bound repo IS that cache; no setters, so the scope cannot drift
  mid-request) and injects the scope into every read. The repo EXECUTES
  internally (:meth:`fetch_all`): consumers are never handed the raw
  connection, because a repo that only built statements would force callers
  to hold a connection to run them — making the DR-006 bypass the normal
  path instead of a fenced-off one. The connection attribute is name-mangled
  private; reaching it is a deliberate act, not a convenience.
- ``BuildScopedWriter`` — the WRITE capability, a separate type: §27.1 writes
  always target a ``building`` build, so :meth:`~BuildScopedWriter.insert`
  exists ONLY on instances that came through the validating
  :meth:`~BuildScopedWriter.for_building_build` factory. An active-bound repo
  has no insert method to misuse — the live snapshot's immutability is a
  property of the type, not a runtime flag.
- The scope column map is explicit: tables that are deliberately NOT
  build-scoped (``builds`` itself, ``review_ledger`` per DR-003, the
  observability tables with their own §27.7 binding rules) and tables scoped
  only transitively (``entity_mentions`` hang off entities, §4 gives them no
  build_id) are rejected loudly — silently "scoping" them would fake the
  guarantee this layer exists to give.

The read/write surface is deliberately minimal (filtered fetch, scoped
insert); C4/C6/BA3 extend it additively as their access patterns land.
Neo4j/Qdrant projections get the same treatment in C1c/C1d (DR-004's
``WHERE n.build_id`` / payload filter); this module is Postgres-only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import coercions, operators, roles, visitors

from core.stores import tables

#: §4 core tables carrying BOTH scope columns — reads filter and writes inject
#: (project, build_id).
PROJECT_AND_BUILD_SCOPED = (
    tables.documents,
    tables.entities,
    tables.relations,
    tables.community_reports,
    tables.merge_candidates,
)

#: §4 core tables carrying only build_id (their project is derivable through
#: the composite FK parent) — reads filter and writes inject build_id.
BUILD_ONLY_SCOPED = (
    tables.chunks,
    tables.relation_evidence,
)

_SCOPE_COLUMNS: dict[sa.Table, tuple[str, ...]] = {
    **{table: ("project", "build_id") for table in PROJECT_AND_BUILD_SCOPED},
    **{table: ("build_id",) for table in BUILD_ONLY_SCOPED},
}


class NotBuildScopedError(TypeError):
    """Raised when a table must not be silently build-scoped.

    ``builds`` is the scope's source of truth, ``review_ledger`` is
    deliberately cross-build (DR-003), the observability tables have their own
    §27.7 binding rules, and ``entity_mentions`` are scoped transitively
    through their entity (§4 gives them no build_id column). Pretending to
    scope any of these would fake the DR-006 guarantee.
    """


class RowNotInBuildError(LookupError):
    """A guarded UPDATE/DELETE named a row that is not in this writer's scope.

    The statement's WHERE carries the full (id, project?, build_id) scope plus
    the building-status guard; zero rows while the build is still ``building``
    means the id names a row of another build/project (or nothing) — mutating
    across scopes is the exact thing DR-006 exists to prevent, so it fails
    typed instead of silently doing nothing.
    """

    def __init__(self, table: str, row_id: uuid.UUID, project: str, build_id: uuid.UUID) -> None:
        super().__init__(
            f"{table} row {row_id} is not in project {project!r} build {build_id} "
            "— cross-scope mutation refused"
        )
        self.table = table
        self.row_id = row_id
        self.project = project
        self.build_id = build_id


class MentionTargetNotInBuildError(LookupError):
    """The entity an ``entity_mentions`` row targets is not in this build.

    ``entity_mentions`` is scoped through its parent entity (§4). When
    :meth:`BuildScopedWriter.insert_entity_mention` writes zero rows AND the
    build is still ``building``, the cause is the parent, not the build: the
    ``entity_id`` names no entity of this writer's ``(project, build_id)`` (a
    wrong-build entity, or an unknown id), so the mention would cross scopes.
    (If the build has stopped being ``building``, ``BuildNotWritableError`` is
    raised instead — the two causes are told apart, never conflated into a
    self-contradictory "not a 'building' build (found status: 'building')".)
    """

    def __init__(self, project: str, build_id: uuid.UUID, entity_id: uuid.UUID) -> None:
        super().__init__(
            f"entity {entity_id} is not an entity of project {project!r} "
            f"build {build_id} — a mention cannot attach across builds"
        )
        self.project = project
        self.build_id = build_id
        self.entity_id = entity_id


class NoActiveBuildError(LookupError):
    """No ``builds.status='active'`` row for the project (DR-001).

    The API layer maps this to the frozen ``NO_ACTIVE_BUILD`` error code
    (§15 / §27.2); core raises it typed instead of guessing a build.
    """

    def __init__(self, project: str) -> None:
        super().__init__(f"no active build for project {project!r}")
        self.project = project


class BuildNotWritableError(LookupError):
    """The requested write binding is not this project's ``building`` build.

    §27.1: 寫入一律指定 building 的 build_id — every other status is an
    immutable snapshot (writing into ``active`` would mutate live data;
    another project's build would cross scopes). ``status`` is None when the
    build id does not exist at all.
    """

    def __init__(self, project: str, build_id: uuid.UUID, status: str | None) -> None:
        super().__init__(
            f"build {build_id} is not a 'building' build of project {project!r} "
            f"(found status: {status!r})"
        )
        self.project = project
        self.build_id = build_id
        self.status = status


#: Module-private construction token: the factories below are the only
#: sanctioned ways to bind a scope (the active build for reads, a VALIDATED
#: building build for writes) — an unvalidated direct construction would
#: reopen the bind-to-anything hole.
_CONSTRUCTION_TOKEN = object()


#: The characters a PostgreSQL operator name may contain (PG manual §4.1.3).
#: SQLAlchemy renders dialect operators (JSONB ``->>``/``@>``/``?``, array
#: ``&&`` …) as ``custom_op`` nodes whose opstring is drawn ENTIRELY from this
#: set — and an opstring of only these characters cannot contain a space, a
#: keyword (``OR``), a quote, or a ``)``, so it cannot restructure the boolean
#: expression to escape the scope. An ``op("...")`` payload that could escape
#: (``") OR true OR ("``, ``"= 'x' OR true"``) necessarily uses characters
#: OUTSIDE this set, which is exactly what we reject.
_PG_OPERATOR_CHARS = frozenset("+-*/<>=~!@#%^&|`?")


def _is_unsafe_custom_op(candidate: object) -> bool:
    """True if ``candidate`` is a ``custom_op`` with an unsafe opstring.

    ``op()``/``bool_op()`` splice their operator string VERBATIM between the
    operands. A symbol-only opstring is a genuine dialect operator and is safe
    (see :data:`_PG_OPERATOR_CHARS`); anything with a letter, space, quote or
    paren is attacker-controllable raw SQL and is rejected.
    """
    if not isinstance(candidate, operators.custom_op):
        return False
    return not (candidate.opstring and set(candidate.opstring) <= _PG_OPERATOR_CHARS)


def _is_raw_sql_node(node: object) -> bool:
    """True if ``node`` carries attacker-controllable SQL spliced VERBATIM.

    Three sibling APIs splice a raw string with no parameterization and no
    reliable parenthesization, so each can smuggle an ``OR`` / paren-closing
    payload out of the ANDed scope:

    - ``text()`` → :class:`TextClause`;
    - ``literal_column()`` → a ``ColumnClause`` with ``is_literal=True``;
    - ``op("<raw>")`` / ``bool_op("<raw>")`` → a ``custom_op`` (on a node's
      ``operator`` or ``modifier``) whose opstring is NOT a pure operator
      token — the safe dialect operators (``->>``, ``@>``, ``&&`` …) are
      allowed so C4+ can filter JSONB/array columns (see
      :func:`_is_unsafe_custom_op`).

    (Plain ``column()`` and ``func.<name>`` are NOT here: SQLAlchemy quotes
    those identifiers, so a payload lands inside quotes, contained.)
    """
    return (
        isinstance(node, sa.TextClause)
        or getattr(node, "is_literal", False)
        or _is_unsafe_custom_op(getattr(node, "operator", None))
        or _is_unsafe_custom_op(getattr(node, "modifier", None))
    )


def _reject_raw_sql(predicate: sa.ColumnExpressionArgument[bool]) -> None:
    """Refuse any raw-SQL node in a caller predicate — even nested (DR-006).

    SQLAlchemy splices raw SQL (``text()``/``literal_column()`` bodies, and
    ``op()``/``bool_op()`` operator strings) into the WHERE conjunction
    VERBATIM, so the scope's ANDed filters and the caller's predicate share
    one flat boolean level. A raw ``OR`` (or a ``")...--"`` / ``") OR (..."``
    payload that lexically closes an enclosing group) flips precedence and
    reads outside the build scope — verified by compilation:

        build_id = :b AND (documents.mime IS NOT NULL) OR (true ...)

    A top-level check is not enough: ``sa.or_(sa.text(...), col == x)`` (or a
    ``custom_op`` deeper in the tree) hides the raw node one level down, where
    it still escapes. So coerce the predicate exactly as ``.where()`` will
    (which also rejects a bare-string predicate — SQLAlchemy 2.x refuses to
    auto-``text()`` it), then walk the whole expression tree
    (``visitors.iterate``) and reject on the first raw node (see
    :func:`_is_raw_sql_node`). Structural expressions (column comparisons,
    ``sa.or_``/``sa.and_``, ``in_``/``like``/``between``) contain none of
    these and self-group, so they pass and cannot widen the scope.
    """
    element = coercions.expect(roles.WhereHavingRole, predicate)
    for node in visitors.iterate(element):
        if _is_raw_sql_node(node):
            raise TypeError(
                "raw-SQL predicates are not accepted, even nested inside "
                "or_/and_: text()/literal_column() bodies and op()/bool_op() "
                "operator strings are spliced verbatim, so an OR (or a "
                "grouping-closing ')...--' payload) inside would escape the "
                "build scope; use structural expressions only (column "
                "comparisons, sa.or_/sa.and_, in_/like/between)"
            )


async def active_build_id(conn: AsyncConnection, project: str) -> uuid.UUID:
    """DR-001: the single-query active-build lookup (§27.1).

    At most one row can match — ``one_active_build`` is a partial unique
    index, so "the" active build is a database invariant, not a convention.
    """
    result = (
        await conn.execute(
            sa.select(tables.builds.c.id).where(
                tables.builds.c.project == project,
                tables.builds.c.status == "active",
            )
        )
    ).scalar_one_or_none()
    if result is None:
        raise NoActiveBuildError(project)
    build: uuid.UUID = result
    return build


class BuildScopedRepo:
    """Read-only Postgres access bound to one ``(project, build_id)`` (DR-006).

    Construct via :meth:`for_active_build` (the normal query/Console path).
    The scope is read-only after construction, and execution happens inside
    the repo — a consumer holding a repo holds no raw connection to escape
    through, and no write method exists on this type (see
    :class:`BuildScopedWriter`).
    """

    __slots__ = ("__conn", "__project", "__build_id")

    def __init__(
        self,
        conn: AsyncConnection,
        project: str,
        build_id: uuid.UUID,
        *,
        _token: object = None,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError(
                "construct via BuildScopedRepo.for_active_build (reads) or "
                "BuildScopedWriter.for_building_build (pipeline writes) — direct "
                "construction would skip the scope validation those factories do"
            )
        # name-mangled: reaching the connection from outside is a deliberate
        # bypass (visible in review), never an accident of convenience
        self.__conn = conn
        self.__project = project
        self.__build_id = build_id

    @property
    def project(self) -> str:
        return self.__project

    @property
    def build_id(self) -> uuid.UUID:
        return self.__build_id

    @classmethod
    async def for_active_build(cls, conn: AsyncConnection, project: str) -> BuildScopedRepo:
        """Bind to the project's active build (one lookup, then cached here).

        Always returns the read-only type — pinned explicitly (not ``cls``)
        so a subclass can never be tricked into an active-bound WRITER.
        """
        build = await active_build_id(conn, project)
        return BuildScopedRepo(conn, project, build, _token=_CONSTRUCTION_TOKEN)

    # -- scope plumbing (single-underscore: shared with the SQL-shape tests) --

    def _scope_columns(self, table: sa.Table) -> tuple[str, ...]:
        try:
            return _SCOPE_COLUMNS[table]
        except KeyError:
            raise NotBuildScopedError(
                f"table {table.name!r} is not directly build-scoped — "
                "see core.stores.repo docstring for why it is excluded"
            ) from None

    def _scope_values(self, table: sa.Table) -> dict[str, Any]:
        values: dict[str, Any] = {"build_id": self.__build_id}
        if "project" in self._scope_columns(table):
            values["project"] = self.__project
        return values

    def _select(self, table: sa.Table) -> sa.Select[Any]:
        query = sa.select(table)
        for column, value in self._scope_values(table).items():
            query = query.where(table.c[column] == value)
        return query

    def _insert_values(self, table: sa.Table, values: dict[str, Any]) -> sa.Insert:
        scope = self._scope_values(table)
        conflicts = {
            key: values[key] for key in scope.keys() & values.keys() if values[key] != scope[key]
        }
        if conflicts:
            raise ValueError(
                f"scope columns {sorted(conflicts)} conflict with this repo's binding "
                f"(project={self.__project!r}, build_id={self.__build_id!r})"
            )
        payload = {**values, **scope}
        # §27.1's "writes target a building build" must hold PER STATEMENT, not
        # just at bind time: a build activated/failed after for_building_build
        # validated it must not silently keep absorbing writes (TOCTOU). The
        # status recheck is folded INTO the insert (INSERT .. SELECT .. WHERE
        # EXISTS), so check and write are one atomic statement — and FOR SHARE
        # on the builds row makes in-flight writes and the activation UPDATE
        # mutually exclusive at the row-lock level (verified on live Postgres:
        # activation blocks until the writing transaction ends, and a write
        # after a committed activation inserts zero rows).
        guard = (
            sa.select(tables.builds.c.id)
            .where(
                tables.builds.c.id == self.__build_id,
                tables.builds.c.project == self.__project,
                tables.builds.c.status == "building",
            )
            .with_for_update(read=True)
        )
        row = sa.select(
            *[
                sa.bindparam(key, value, type_=table.c[key].type).label(key)
                for key, value in payload.items()
            ]
        ).where(sa.exists(guard))
        return table.insert().from_select(list(payload), row)

    async def _execute(self, statement: sa.Executable) -> sa.CursorResult[Any]:
        # the sole execution seam — defined here because the mangled __conn is
        # unreachable from subclasses (that unreachability is the point)
        return await self.__conn.execute(statement)

    # -- the public, executing surface ----------------------------------------

    async def fetch_all(
        self, table: sa.Table, *where: sa.ColumnExpressionArgument[bool]
    ) -> Sequence[sa.Row[Any]]:
        """Read the scoped rows, optionally narrowed by caller predicates.

        Predicates can only narrow — the scope filters are already in the
        query and nothing the caller passes can remove them, PROVIDED no raw
        SQL sneaks in (see :func:`_reject_raw_sql` for why raw text escapes
        the scope and why the check must recurse into ``or_``/``and_``).
        """
        query = self._select(table)
        for predicate in where:
            _reject_raw_sql(predicate)
            query = query.where(predicate)
        return (await self._execute(query)).fetchall()

    async def mention_refs(self) -> set[tuple[uuid.UUID, str]]:
        """The ``(entity_id, source_ref)`` mention pairs already in this build.

        ``entity_mentions`` has no ``build_id`` (§4) — it is scoped through its
        parent entity — so this read joins to ``entities`` and filters by the
        bound ``(project, build_id)``, staying within the DR-006 scope. Graph
        extraction uses it to stay idempotent across re-runs (§5): a mention
        of an existing (entity, source) is not written twice.
        """
        mentions = tables.entity_mentions
        entities = tables.entities
        query = (
            sa.select(mentions.c.entity_id, mentions.c.source_ref)
            .select_from(mentions.join(entities, entities.c.id == mentions.c.entity_id))
            .where(
                entities.c.project == self.project,
                entities.c.build_id == self.build_id,
            )
        )
        rows = (await self._execute(query)).fetchall()
        return {(row.entity_id, row.source_ref) for row in rows}

    async def mentions_by_entity(
        self, entity_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[str, str]]]:
        """``(source_kind, source_ref)`` mentions per entity, scoped through the
        parent entity (§4: ``entity_mentions`` has no build_id of its own).

        C6a builds §27.2 entity source_refs from these — an entity result must
        cite ≥1 mention (chunk or row), and only the ``source_kind`` tells the
        two apart (``text`` mention → a chunk ref, ``structured`` → a row ref).
        The ``entity_id.in_`` filter keeps it to the hits being enriched; the
        ``entities`` join filters the bound ``(project, build_id)`` so a mention
        of another build's entity can never leak in (DR-006).

        Only ``status == 'active'`` entities are enriched: the index step
        projects active entities only, but projection is forward-only, so a
        point for an entity that resolution later moved OFF ``active`` (to any
        of rejected/merged/needs_review/deprecated) can outlive its exclusion.
        Re-checking the SoR here means such a stale hit resolves to zero
        mentions, so :func:`_entity_result` drops it as projection drift
        (§19/§22) rather than surfacing a non-active entity as a production
        result — the same SoR re-verification chunk hits already get (their row
        must still exist).
        """
        if not entity_ids:
            return {}
        mentions = tables.entity_mentions
        entities = tables.entities
        query = (
            sa.select(mentions.c.entity_id, mentions.c.source_kind, mentions.c.source_ref)
            .select_from(mentions.join(entities, entities.c.id == mentions.c.entity_id))
            .where(
                entities.c.project == self.project,
                entities.c.build_id == self.build_id,
                entities.c.status == "active",
                mentions.c.entity_id.in_(entity_ids),
            )
        )
        rows = (await self._execute(query)).fetchall()
        grouped: dict[uuid.UUID, list[tuple[str, str]]] = {}
        for row in rows:
            grouped.setdefault(row.entity_id, []).append((row.source_kind, row.source_ref))
        return grouped

    async def entity_ids_by_name(self, name: str) -> list[uuid.UUID]:
        """Active entity ids whose canonical_name matches ``name`` (SoR seed
        resolution for C6c's graph templates — the traversal seed is resolved
        in POSTGRES, never by trusting the projection).

        Matching is case-insensitive via ``lower()`` — deliberately simpler
        than the §27.3 fingerprint ``norm`` (NFKC + casefold), which exists for
        identity minting, not lookup ergonomics. Several ids can share a name
        (distinct disambiguators are distinct entities — §27.3); the caller
        gets them all, deterministically ordered."""
        entities = tables.entities
        query = (
            sa.select(entities.c.id)
            .where(
                entities.c.project == self.project,
                entities.c.build_id == self.build_id,
                entities.c.status == "active",
                sa.func.lower(entities.c.canonical_name) == name.lower(),
            )
            .order_by(entities.c.id)
        )
        rows = (await self._execute(query)).fetchall()
        return [row.id for row in rows]

    async def active_entity_ids(self, entity_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """The subset of ``entity_ids`` that is ACTIVE in the SoR — §19
        projection-drift re-verification for graph traversal results: the
        forward-only Neo4j projection can hold nodes whose entity resolution
        later moved off ``active``, and those must read as drift, not results
        (the C6a lesson, applied to the graph read face)."""
        if not entity_ids:
            return set()
        entities = tables.entities
        query = sa.select(entities.c.id).where(
            entities.c.project == self.project,
            entities.c.build_id == self.build_id,
            entities.c.status == "active",
            entities.c.id.in_(list(entity_ids)),
        )
        rows = (await self._execute(query)).fetchall()
        return {row.id for row in rows}

    async def relations_with_evidence(
        self, triples: Sequence[tuple[uuid.UUID, uuid.UUID, str]]
    ) -> dict[tuple[uuid.UUID, uuid.UUID, str], tuple[uuid.UUID, list[dict[str, Any]]]]:
        """Resolve projected edges back to their SoR relation + evidence rows.

        Keyed by ``(src_entity_id, dst_entity_id, type)`` — the identity a
        projected ``[:REL]`` edge carries (§4). Only ``status == 'active'``
        relations resolve (the same drift rule as entities); an edge that
        resolves to nothing is stale projection and the caller drops it. Each
        value is ``(relation_id, evidence rows)`` — the §27.2/§27.4 citation
        payload (evidence_type/evidence_ref/chunk_id/offsets/quote/source_uri),
        build-aligned through the evidence's own ``build_id``."""
        if not triples:
            return {}
        relations = tables.relations
        evidence = tables.relation_evidence
        match = sa.tuple_(
            relations.c.src_entity_id, relations.c.dst_entity_id, relations.c.type
        ).in_(list(triples))
        rel_rows = (
            await self._execute(
                sa.select(
                    relations.c.id,
                    relations.c.src_entity_id,
                    relations.c.dst_entity_id,
                    relations.c.type,
                ).where(
                    relations.c.project == self.project,
                    relations.c.build_id == self.build_id,
                    relations.c.status == "active",
                    match,
                )
            )
        ).fetchall()
        by_id: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, str]] = {
            row.id: (row.src_entity_id, row.dst_entity_id, row.type) for row in rel_rows
        }
        if not by_id:
            return {}
        ev_rows = (
            await self._execute(
                sa.select(
                    evidence.c.relation_id,
                    evidence.c.evidence_type,
                    evidence.c.evidence_ref,
                    evidence.c.chunk_id,
                    evidence.c.start_offset,
                    evidence.c.end_offset,
                    evidence.c.quote,
                    evidence.c.source_uri,
                ).where(
                    evidence.c.build_id == self.build_id,
                    evidence.c.relation_id.in_(list(by_id.keys())),
                )
            )
        ).fetchall()
        grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for row in ev_rows:
            grouped.setdefault(row.relation_id, []).append(
                {
                    "evidence_type": row.evidence_type,
                    "evidence_ref": row.evidence_ref,
                    "chunk_id": row.chunk_id,
                    "start_offset": row.start_offset,
                    "end_offset": row.end_offset,
                    "quote": row.quote,
                    "source_uri": row.source_uri,
                }
            )
        return {triple: (rel_id, grouped.get(rel_id, [])) for rel_id, triple in by_id.items()}


class BuildScopedWriter(BuildScopedRepo):
    """The pipeline write capability (§27.1: writes target a building build).

    Exists ONLY via :meth:`for_building_build` — the validating factory is
    the type's sole entry, so "this object can insert" and "this scope is a
    verified building build of this project" are the same fact. Because that
    fact can stop being true AFTER binding (the build activates, fails, or is
    archived), every :meth:`insert` also revalidates it inside the statement
    itself — the bind-time check is ergonomics (fail early, typed); the
    per-statement guard is the invariant. Writers also read (their own build:
    skip/rerun decisions need it).
    """

    __slots__ = ()

    @classmethod
    async def for_building_build(
        cls, conn: AsyncConnection, project: str, build_id: uuid.UUID
    ) -> BuildScopedWriter:
        """Bind a pipeline writer to a VALIDATED ``building`` build (§27.1).

        The id must name an existing build, of THIS project, in status
        ``building`` — anything else (the active build, another project's
        build, a finished snapshot, a typo'd id) raises the typed
        ``BuildNotWritableError`` instead of silently landing writes where
        they would mutate live data or cross scopes.
        """
        status = (
            await conn.execute(
                sa.select(tables.builds.c.status).where(
                    tables.builds.c.id == build_id,
                    tables.builds.c.project == project,
                )
            )
        ).scalar_one_or_none()
        if status != "building":
            raise BuildNotWritableError(project, build_id, status)
        return BuildScopedWriter(conn, project, build_id, _token=_CONSTRUCTION_TOKEN)

    async def insert(self, table: sa.Table, /, **values: Any) -> None:
        """Insert a row with the scope columns injected.

        A caller-supplied conflicting scope value is a bug by definition —
        rejected loudly rather than silently overwritten (either direction
        would hide a cross-build write).

        The bind-time validation is revalidated PER INSERT, inside the
        statement itself (see ``_insert_values``): if the build stopped being
        ``building`` after this writer was bound — activated to the live
        snapshot, failed, archived — the insert lands zero rows and raises the
        same typed ``BuildNotWritableError`` instead of silently mutating a
        now-immutable build. §27.1's guarantee is per statement, not
        per binding.
        """
        result = await self._execute(self._insert_values(table, values))
        if result.rowcount == 0:
            status: str | None = (
                await self._execute(
                    sa.select(tables.builds.c.status).where(
                        tables.builds.c.id == self.build_id,
                        tables.builds.c.project == self.project,
                    )
                )
            ).scalar_one_or_none()
            raise BuildNotWritableError(self.project, self.build_id, status)

    async def insert_entity_mention(
        self,
        *,
        entity_id: uuid.UUID,
        source_kind: str,
        source_ref: str,
        surface_form: str | None,
        confidence: float | None,
    ) -> None:
        """Insert an ``entity_mentions`` row, scoped THROUGH its parent entity.

        ``entity_mentions`` carries no ``build_id`` (§4): its scope is the
        entity it hangs off, so it cannot go through :meth:`insert` (that would
        try to inject a build_id column the table lacks — ``NotBuildScopedError``).
        The scope invariant is instead enforced structurally here: the row
        lands ONLY if the named ``entity_id`` is an entity of THIS writer's
        ``(project, build_id)`` whose build is still ``building`` — the same
        atomic ``INSERT .. SELECT .. WHERE EXISTS(... FOR SHARE)`` guard
        :meth:`insert` uses, so a mention cannot attach to another build's
        entity, an unknown id, or a build that activated after this writer was
        bound (TOCTOU — the ``FOR SHARE`` on the build row is mutually
        exclusive with the activation UPDATE). Zero rows has two causes, told
        apart so the error names the real one: a build that stopped being
        ``building`` (activated/failed) ⇒ ``BuildNotWritableError``; a build
        still ``building`` but a parent ``entity_id`` outside this scope
        (wrong build, unknown id) ⇒ ``MentionTargetNotInBuildError``.
        """
        guard = (
            sa.select(tables.entities.c.id)
            .select_from(
                tables.entities.join(
                    tables.builds, tables.builds.c.id == tables.entities.c.build_id
                )
            )
            .where(
                tables.entities.c.id == entity_id,
                tables.entities.c.project == self.project,
                tables.entities.c.build_id == self.build_id,
                tables.builds.c.status == "building",
            )
            .with_for_update(read=True, of=tables.builds)
        )
        payload: dict[str, Any] = {
            "id": uuid.uuid4(),
            "entity_id": entity_id,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "surface_form": surface_form,
            "confidence": confidence,
        }
        row = sa.select(
            *[
                sa.bindparam(key, value, type_=tables.entity_mentions.c[key].type).label(key)
                for key, value in payload.items()
            ]
        ).where(sa.exists(guard))
        result = await self._execute(
            tables.entity_mentions.insert().from_select(list(payload), row)
        )
        if result.rowcount == 0:
            # Zero rows has two distinct causes; disambiguate so the error names
            # the real one. If the build is no longer 'building', that's the
            # (activated/failed) writability failure; otherwise the parent
            # entity_id is simply not in this build's scope.
            status: str | None = (
                await self._execute(
                    sa.select(tables.builds.c.status).where(
                        tables.builds.c.id == self.build_id,
                        tables.builds.c.project == self.project,
                    )
                )
            ).scalar_one_or_none()
            if status != "building":
                raise BuildNotWritableError(self.project, self.build_id, status)
            raise MentionTargetNotInBuildError(self.project, self.build_id, entity_id)

    # -- resolution mutations (C4) — same per-statement guard as insert -------

    def _scoped_where(self, table: sa.Table, row_id: uuid.UUID) -> list[sa.ColumnElement[bool]]:
        """The full scope predicate for one row, plus the atomic building-
        status guard (EXISTS ... FOR SHARE — mutually exclusive with the
        activation UPDATE's row lock, same proof as ``_insert_values``)."""
        guard = (
            sa.select(tables.builds.c.id)
            .where(
                tables.builds.c.id == self.build_id,
                tables.builds.c.project == self.project,
                tables.builds.c.status == "building",
            )
            .with_for_update(read=True)
        )
        where: list[sa.ColumnElement[bool]] = [table.c.id == row_id, sa.exists(guard)]
        for column, value in self._scope_values(table).items():
            where.append(table.c[column] == value)
        return where

    async def _raise_zero_rows(self, table: sa.Table, row_id: uuid.UUID) -> None:
        """Zero rows has two causes — name the real one (the C3a lesson)."""
        status: str | None = (
            await self._execute(
                sa.select(tables.builds.c.status).where(
                    tables.builds.c.id == self.build_id,
                    tables.builds.c.project == self.project,
                )
            )
        ).scalar_one_or_none()
        if status != "building":
            raise BuildNotWritableError(self.project, self.build_id, status)
        raise RowNotInBuildError(table.name, row_id, self.project, self.build_id)

    async def update(self, table: sa.Table, row_id: uuid.UUID, /, **values: Any) -> None:
        """Mutate one scoped row (resolution: statuses, re-pointed endpoints,
        re-minted signatures/hashes — DESIGN §7/§17).

        Scope columns can never be changed (a "move this row to another
        build" is a cross-build write in disguise); the row must be in THIS
        writer's scope and the build still ``building`` — both enforced
        inside the statement itself, like :meth:`insert`.
        """
        forbidden = set(values) & set(self._scope_columns(table))
        if forbidden:
            raise ValueError(
                f"scope columns {sorted(forbidden)} cannot be updated — "
                "re-scoping a row is a cross-build write"
            )
        statement = table.update().where(*self._scoped_where(table, row_id)).values(**values)
        if (await self._execute(statement)).rowcount == 0:
            await self._raise_zero_rows(table, row_id)

    async def delete(self, table: sa.Table, row_id: uuid.UUID, /) -> None:
        """Delete one scoped row — exists ONLY for resolution's true-duplicate
        evidence case (§27.4 dedup: after a merge re-mints a signature, an
        evidence row whose re-hash collides with an already-stored twin is an
        exact duplicate; its twin carries the identical provenance). Same
        atomic scope + building guard as :meth:`update`.
        """
        statement = table.delete().where(*self._scoped_where(table, row_id))
        if (await self._execute(statement)).rowcount == 0:
            await self._raise_zero_rows(table, row_id)

    async def repoint_mentions(self, from_entity: uuid.UUID, to_entity: uuid.UUID) -> int:
        """Re-point a merged entity's mentions onto the canonical (§7).

        Both endpoints must be entities of THIS build (validated inside the
        statement — a mention must never come to reference another build's
        entity) and the build still ``building``. Zero rows is legitimate
        (the loser may have no mentions), so the scope precondition is
        checked EXPLICITLY first rather than inferred from rowcount.
        """
        for entity_id in (from_entity, to_entity):
            in_scope = (
                await self._execute(
                    sa.select(tables.entities.c.id).where(
                        tables.entities.c.id == entity_id,
                        tables.entities.c.project == self.project,
                        tables.entities.c.build_id == self.build_id,
                    )
                )
            ).scalar_one_or_none()
            if in_scope is None:
                raise MentionTargetNotInBuildError(self.project, self.build_id, entity_id)
        guard = (
            sa.select(tables.builds.c.id)
            .where(
                tables.builds.c.id == self.build_id,
                tables.builds.c.project == self.project,
                tables.builds.c.status == "building",
            )
            .with_for_update(read=True)
        )
        statement = (
            tables.entity_mentions.update()
            .where(tables.entity_mentions.c.entity_id == from_entity, sa.exists(guard))
            .values(entity_id=to_entity)
        )
        result = await self._execute(statement)
        count = int(result.rowcount or 0)
        if count == 0:
            # no mentions moved — either genuinely none, or the build stopped
            # being writable mid-flight; tell those apart before returning
            status: str | None = (
                await self._execute(
                    sa.select(tables.builds.c.status).where(
                        tables.builds.c.id == self.build_id,
                        tables.builds.c.project == self.project,
                    )
                )
            ).scalar_one_or_none()
            if status != "building":
                raise BuildNotWritableError(self.project, self.build_id, status)
        return count
