"""Why: universal DDL claims ("every FK has a covering index") are only real
when the INSTANCES are enumerated from the schema itself — a hand-maintained
list silently exempts everything it forgot (class 1/#17: the predecessor FK
test enumerated 7 of the 12 FK groups and its own docstring admitted the
false-green risk).

Two suites, one exemption corpus (class 16 — two gates must accept the same
set, provable only by shared inputs):

* the OFFLINE half walks ``core.stores.tables`` metadata — the single source
  migrations are rendered from — and runs every iteration;
* the INTEGRATION half asks ``pg_catalog`` on the migrated database. Its
  property queries alone would be VACUOUSLY green for objects a migration
  forgot to create (local codex P1), so it also asserts instance-set PARITY
  (tables/columns/PKs/FKs/uniques: metadata ⊆/= live), and carries an
  in-suite probe table proving each enumeration query can actually flag a
  violator (a broken query returning [] must fail the probe, not pass the
  suite — guards.md's dual). The probe stays inside one rollback-only
  transaction, so no run — killed, failed, or concurrent — can leave it
  behind or show it to other connections.

Exemptions are DESIGN decisions, not test conveniences: each entry names its
anchor, and a new violation must either fix the DDL or land here with its own
justification.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import UnaryExpression

from core.config import get_settings
from core.stores.tables import metadata, review_ledger

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- shared exemption corpus (consumed by BOTH suites) ----------------------------

#: (table, column) pairs allowed to be NULLABLE while a member of a UNIQUE
#: index, with the EXACT partial predicate (normalized) that legalizes each:
#: uniqueness must be scoped to `col IS NOT NULL` — a substring test would
#: also bless `... OR TRUE` or a predicate on some other column (local codex
#: P2). relations.relation_signature: §27.3 — C3 stages rows before C4 mints
#: signatures, so NULL means "not yet minted" and uniqueness applies only to
#: minted signatures.
EXEMPT_PREDICATES: dict[tuple[str, str], str] = {
    ("relations", "relation_signature"): "relation_signature is not null",
}

#: (table, index-name) pairs allowed to have EXPRESSION members in a UNIQUE
#: index. An expression's nullability cannot be introspected without parsing
#: SQL text (class 14 — we don't), and Postgres stores attnum 0 for the slot,
#: so both halves would otherwise silently SKIP the member (Codex #115 R2: a
#: unique over ``lower(email)`` with nullable email admits repeated NULL keys
#: unseen). Every expression unique must therefore be exempted BY NAME with a
#: nullability justification, or the suite fails loudly.
#: merge_candidates_pair_unique: LEAST/GREATEST over left/right_entity_id —
#: both NOT NULL (pinned by the column tests), so no NULL key can form.
EXPRESSION_UNIQUES_OK: set[tuple[str, str]] = {
    ("merge_candidates", "merge_candidates_pair_unique"),
}


def _normalized_predicate(text: object) -> str:
    """Collapse the cosmetic differences between metadata text and
    ``pg_get_expr`` rendering: parens, casing, whitespace, and the type
    casts pg adds (``'active'::text``) — metadata says ``status = 'active'``,
    the catalog says ``((status = 'active'::text))``. Single-word cast names
    only (no ``double precision`` here; a new multi-word cast would surface
    as a loud parity mismatch, not a silent pass)."""
    stripped = re.sub(r"::[a-z_]+", "", str(text))
    return re.sub(r"[\s()]+", " ", stripped).strip().lower()


#: Frozen identity keys (DESIGN §27.3/§27.4) that must each be pinned by a
#: UNIQUE index/constraint with exactly these columns. This registry is the
#: SPEC side of a lockstep test — the enumeration side (every unique's
#: nullability, every FK's index) comes from the schema; this dict exists
#: because no catalog can know WHICH columns are identity keys.
IDENTITY_UNIQUES: dict[str, tuple[str, ...]] = {
    "entities": ("project", "build_id", "entity_key"),
    "ontology_proposals": ("project", "proposal_key"),
    "relation_evidence": ("build_id", "evidence_hash"),
    "chunks": ("document_id", "ordinal"),
    "pipeline_step_items": ("step_id", "item_kind", "item_ref"),
}


# --- offline half: enumerate the metadata every iteration -------------------------

#: Build-scoping columns (DR-006): present in many composite FKs but nearly
#: valueless as an index LEADING column — a probe served only by them still
#: scans most of the child table. The house rule (tables.py's
#: relation_evidence_by_relation note: the dedup index "leads with build_id
#: and cannot serve them") is that an FK is supported only by an index whose
#: FIRST column is one of the FK's IDENTITY columns.
SCOPE_COLS = {"project", "build_id"}


def _identity_cols(fk_cols: tuple[str, ...]) -> set[str]:
    """The FK's selective columns; for a pure-scope FK (builds.project →
    projects) the scope column IS the identity."""
    return set(fk_cols) - SCOPE_COLS or set(fk_cols)


def _member_col(expr: object) -> str | None:
    """A member expression's column name, unwrapping SORT modifiers only: a
    ``col.desc()`` member is still the column (live-side ``indkey`` carries
    its real attnum; direction lives in indoption), but a genuine function
    (``lower(col)``/LEAST) is not — Postgres stores attnum 0 for those."""
    if isinstance(expr, sa.Column):
        return str(expr.name)
    if isinstance(expr, UnaryExpression) and isinstance(expr.element, sa.Column):
        return str(expr.element.name)
    return None


def _plain_expr_cols(ix: sa.Index) -> tuple[str, ...] | None:
    """The index's key columns IFF every member is a plain column (sort
    modifiers allowed), else None. ``Index.columns`` is the wrong source
    (local codex P2): it silently drops expression members (merge pair's
    LEAST/GREATEST) and can surface a column referenced INSIDE an expression
    (``lower(col)``) as if the index were on the column itself — the
    expression list is the true member list."""
    cols: list[str] = []
    for expr in ix.expressions:
        name = _member_col(expr)
        if name is None:
            return None
        cols.append(name)
    return tuple(cols)


def _first_index_cols(table: sa.Table) -> set[str]:
    """First columns of the structures that can serve an FK probe. PARTIAL
    indexes are excluded: they only answer queries implying their predicate,
    so e.g. one_active_build(project) WHERE status='active' cannot support
    builds.project lookups over non-active rows (local codex P2 — deleting
    builds_by_project must go RED, not hide behind the partial). Expression
    members disqualify via _plain_expr_cols: an index leading on
    ``lower(col)`` cannot serve an equality probe on ``col``."""
    firsts = {
        cols[0]
        for ix in table.indexes
        if ix.dialect_options["postgresql"]["where"] is None
        and (cols := _plain_expr_cols(ix)) is not None
        and cols
    }
    firsts.add(tuple(table.primary_key.columns.keys())[0])
    firsts.update(
        tuple(c.columns.keys())[0]
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint) and len(c.columns) > 0
    )
    return firsts


def test_expression_index_members_do_not_masquerade_as_columns() -> None:
    """Pin for the helper itself (local codex P2; no live instance exists to
    probe): ``Index.columns`` surfaces a column referenced INSIDE an
    expression (``lower(a)``) as if the index were on the column — the helper
    must refuse functional members entirely, while a sort modifier's member
    IS its column."""
    t = sa.Table("zz_probe_meta", sa.MetaData(), sa.Column("a", sa.Text), sa.Column("b", sa.Text))
    functional = sa.Index("zz_f", sa.func.lower(t.c.a))
    sorted_ix = sa.Index("zz_s", t.c.a, t.c.b.desc())
    assert _plain_expr_cols(functional) is None
    assert _plain_expr_cols(sorted_ix) == ("a", "b")


def test_every_fk_group_has_a_supporting_index() -> None:
    """Postgres doesn't auto-index FK columns: an unsupported FK makes every
    CASCADE delete (and child-by-parent lookup) scan the child table.
    Enumerated from the metadata — every FK on every table, no list to
    forget (the predecessor listed 7 of 12 and admitted the risk)."""
    missing = [
        (table.name, tuple(fk.column_keys))
        for table in metadata.tables.values()
        for fk in table.foreign_key_constraints
        if not (_first_index_cols(table) & _identity_cols(tuple(fk.column_keys)))
    ]
    assert missing == []


def test_every_fk_declares_on_delete_explicitly() -> None:
    """Postgres' default (NO ACTION) is what you get by forgetting, so it can
    never encode a decision. Every FK here must say RESTRICT or CASCADE on
    purpose (which one is per-FK policy, pinned by the topology tests). An
    EXPLICIT "NO ACTION" is rejected too — it renders identically to the
    default in the catalog (confdeltype 'a'), so allowing the spelling here
    while the integration half flags it would split the two gates."""
    silent = [
        (table.name, tuple(fk.column_keys))
        for table in metadata.tables.values()
        for fk in table.foreign_key_constraints
        if fk.ondelete is None or fk.ondelete.upper().replace(" ", "") == "NOACTION"
    ]
    assert silent == []


def test_every_timestamp_is_timezone_aware() -> None:
    """A naive TIMESTAMP silently reinterprets under a session/server TZ
    change; ordering and lease arithmetic (class 4 clock lessons) assume
    instants. All 18 tables use timestamptz today — keep it universal."""
    naive = [
        (table.name, col.name)
        for table in metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, sa.DateTime) and not col.type.timezone
    ]
    assert naive == []


def test_every_table_has_a_primary_key() -> None:
    """A PK-less table cannot be addressed for UPDATE/DELETE replication or
    referenced by an FK — there is no legitimate shape for one here."""
    missing = [
        table.name for table in metadata.tables.values() if len(table.primary_key.columns) == 0
    ]
    assert missing == []


def test_unique_key_columns_are_not_null_or_partial_exempt() -> None:
    """Postgres unique indexes treat NULLs as DISTINCT, so a nullable member
    column silently disables the uniqueness for every NULL-carrying row (the
    no-op-value escape, class 1). The only legal shape for nullable-unique is
    a PARTIAL index whose predicate is exactly `col IS NOT NULL` — pinned per
    pair in the shared exemption corpus. Members are read from
    ``ix.expressions`` (NOT ``Index.columns``, which silently drops
    expression members — Codex #115 R2): an EXPRESSION member's nullability
    is un-introspectable without parsing SQL text (class 14 — we don't), so
    it is a violation unless the index is name-exempted in
    EXPRESSION_UNIQUES_OK with a nullability justification."""
    violations: list[tuple[str, str]] = []
    for table in metadata.tables.values():
        for ix in table.indexes:
            if not ix.unique:
                continue
            where = ix.dialect_options["postgresql"]["where"]
            for expr in ix.expressions:
                name = _member_col(expr)
                if name is None:
                    if (table.name, str(ix.name)) not in EXPRESSION_UNIQUES_OK:
                        violations.append((table.name, f"expression member in {ix.name}"))
                    continue
                if not table.columns[name].nullable:
                    continue
                expected = EXEMPT_PREDICATES.get((table.name, name))
                ok = (
                    expected is not None
                    and where is not None
                    and _normalized_predicate(where) == expected
                )
                if not ok:
                    violations.append((table.name, name))
        for c in table.constraints:
            if not isinstance(c, sa.UniqueConstraint):
                continue
            for name in c.columns.keys():  # noqa: SIM118 — ColumnCollection iteration yields Columns, not names
                if table.columns[name].nullable:
                    # constraints have no partial form — nullable is always a violation
                    violations.append((table.name, name))
    assert violations == []


def _metadata_partial_unique_sets(table: sa.Table) -> set[tuple[frozenset[str], str]]:
    """(column-set, normalized predicate) per PARTIAL plain-column unique —
    the identity a partial unique enforces is its columns AND its scope, so
    parity must compare both (local codex #115: a migration dropping
    relations_by_signature was invisible — the offline half reads metadata,
    and every live query excluded partial indexes)."""
    sets: set[tuple[frozenset[str], str]] = set()
    for ix in table.indexes:
        if not ix.unique:
            continue
        where = ix.dialect_options["postgresql"]["where"]
        if where is None:
            continue
        cols = _plain_expr_cols(ix)
        if cols:
            sets.add((frozenset(cols), _normalized_predicate(where)))
    return sets


def _metadata_plain_unique_sets(table: sa.Table) -> set[frozenset[str]]:
    """Non-partial, plain-column unique column-sets (incl. the PK) — the
    shape both halves can compare exactly. Partial uniques are policy-checked
    by the nullable/exemption test; expression uniques (merge pair) have no
    column-set to compare."""
    sets = {frozenset(table.primary_key.columns.keys())}
    for ix in table.indexes:
        if not ix.unique or ix.dialect_options["postgresql"]["where"] is not None:
            continue
        cols = _plain_expr_cols(ix)
        if cols:
            sets.add(frozenset(cols))
    sets.update(
        frozenset(c.columns.keys()) for c in table.constraints if isinstance(c, sa.UniqueConstraint)
    )
    return sets


def test_frozen_identity_keys_each_have_a_unique() -> None:
    """§27.3/§27.4: the identity registry above must be pinned by real UNIQUE
    constraints — a lost unique turns writer discipline into the only guard
    (DR-006's failure mode)."""
    for table_name, cols in IDENTITY_UNIQUES.items():
        assert frozenset(cols) in _metadata_plain_unique_sets(metadata.tables[table_name]), (
            table_name,
            cols,
        )


def test_review_ledger_target_key_is_deliberately_not_unique() -> None:
    """The one identity key WITHOUT a unique, pinned in that direction: §27.3
    precedence keeps every decision row and resolves 同鍵多筆 by latest
    decided_at (DR-003 audit trail). A well-meant unique here would silently
    turn append-history into upsert."""
    assert all(not ix.unique for ix in review_ledger.indexes)
    assert not any(isinstance(c, sa.UniqueConstraint) for c in review_ledger.constraints)
    lookup = {str(ix.name): tuple(ix.columns.keys()) for ix in review_ledger.indexes}
    assert lookup["review_ledger_lookup"] == (
        "project",
        "target_kind",
        "target_key",
        "fingerprint_version",
    )


# --- integration half: enumerate the live catalog ---------------------------------
#
# Query conventions (local codex batch): app relations are relkind IN
# ('r','p') — ordinary + partitioned roots — applied uniformly so every query
# sees the same population; names come from pg_class.relname (regclass::text
# would quote mixed-case names and break the shared-corpus comparisons); index
# members are read from the KEY slots only (indkey[0 .. indnkeyatts-1] —
# INCLUDE columns are not uniqueness members), and an FK-supporting index must
# be applicable to arbitrary probes: non-partial (indpred IS NULL) and usable
# (indisvalid/indisready/indislive).

APP_RELKIND_FILTER = "c.relkind IN ('r', 'p')"

# mirrors _identity_cols/_first_index_cols: an FK is supported only by a
# usable, non-partial index whose FIRST column is one of the FK's identity
# (non-scope) columns
FK_WITHOUT_COVERING_INDEX_SQL = f"""
WITH fk AS (
  SELECT con.conrelid, con.conname, c.relname, con.conkey,
         COALESCE(
           (SELECT array_agg(k.a)
              FROM unnest(con.conkey) AS k(a)
              JOIN pg_attribute att
                ON att.attrelid = con.conrelid AND att.attnum = k.a
             WHERE att.attname NOT IN ('project', 'build_id')),
           con.conkey
         ) AS identity_cols
  FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.conrelid
  WHERE con.contype = 'f'
    AND con.connamespace = 'public'::regnamespace
    AND {APP_RELKIND_FILTER}
)
SELECT fk.relname AS tbl, fk.conname AS name
FROM fk
WHERE NOT EXISTS (
  SELECT 1 FROM pg_index i
  WHERE i.indrelid = fk.conrelid
    AND i.indpred IS NULL
    AND i.indisvalid AND i.indisready AND i.indislive
    AND i.indkey[0] = ANY (fk.identity_cols)
)
ORDER BY 1, 2
"""

FK_WITH_DEFAULT_ON_DELETE_SQL = f"""
SELECT c.relname AS tbl, con.conname AS name
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
WHERE con.contype = 'f'
  AND con.connamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND con.confdeltype = 'a'
ORDER BY 1, 2
"""

NAIVE_TIMESTAMP_SQL = f"""
SELECT c.relname AS tbl, a.attname AS name
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND a.attnum > 0 AND NOT a.attisdropped
  AND a.atttypid = 'timestamp'::regtype
ORDER BY 1, 2
"""

TABLE_WITHOUT_PK_SQL = f"""
SELECT c.relname AS tbl, c.relname AS name
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND NOT EXISTS (
    SELECT 1 FROM pg_constraint p WHERE p.conrelid = c.oid AND p.contype = 'p'
  )
ORDER BY 1
"""

# raw facts only — the shared Python-side corpus applies the exemption policy,
# so both suites judge by the same constants; predicate TEXT is fetched so the
# check pins the exact `col IS NOT NULL` shape, same as the offline half
NULLABLE_UNIQUE_MEMBER_SQL = f"""
SELECT c.relname AS tbl, a.attname AS name,
       pg_get_expr(i.indpred, i.indrelid) AS pred
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
CROSS JOIN LATERAL generate_series(0, i.indnkeyatts - 1) AS g(k)
JOIN pg_attribute a
  ON a.attrelid = i.indrelid AND a.attnum = i.indkey[g.k]
WHERE c.relnamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND i.indisunique
  AND NOT a.attnotnull
ORDER BY 1, 2
"""

# every usable, non-partial unique index's KEY column-name set (+ key-slot
# count, so Python can drop rows where an expression member vanished from the
# join instead of silently matching a smaller set)
UNIQUE_INDEX_COLUMN_SETS_SQL = f"""
SELECT c.relname AS tbl, i.indnkeyatts AS nkeys,
       (SELECT array_agg(att.attname ORDER BY att.attname)
          FROM generate_series(0, i.indnkeyatts - 1) AS g(k)
          JOIN pg_attribute att
            ON att.attrelid = i.indrelid AND att.attnum = i.indkey[g.k]) AS cols
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
WHERE c.relnamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND i.indisunique
  AND i.indpred IS NULL
  AND i.indisvalid
ORDER BY 1
"""

# UNIQUE indexes containing EXPRESSION key slots (indkey[slot] = 0): their
# member nullability is invisible to the attribute joins above, so each must
# be name-exempted in EXPRESSION_UNIQUES_OK or fail loudly (Codex #115 R2)
EXPRESSION_UNIQUE_SQL = f"""
SELECT c.relname AS tbl, cl.relname AS name
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_class cl ON cl.oid = i.indexrelid
WHERE c.relnamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND i.indisunique
  AND EXISTS (
    SELECT 1 FROM generate_series(0, i.indnkeyatts - 1) AS g(k)
    WHERE i.indkey[g.k] = 0
  )
ORDER BY 1, 2
"""

# PARTIAL unique indexes with their predicates — excluded from
# UNIQUE_INDEX_COLUMN_SETS_SQL on purpose (a partial unique is not a plain
# identity), so they need their own parity channel (local codex #115)
PARTIAL_UNIQUE_INDEX_SQL = f"""
SELECT c.relname AS tbl, i.indnkeyatts AS nkeys,
       (SELECT array_agg(att.attname ORDER BY att.attname)
          FROM generate_series(0, i.indnkeyatts - 1) AS g(k)
          JOIN pg_attribute att
            ON att.attrelid = i.indrelid AND att.attnum = i.indkey[g.k]) AS cols,
       pg_get_expr(i.indpred, i.indrelid) AS pred
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
WHERE c.relnamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND i.indisunique
  AND i.indpred IS NOT NULL
  AND i.indisvalid
ORDER BY 1
"""

LIVE_COLUMNS_SQL = f"""
SELECT c.relname AS tbl, a.attname AS name
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY 1, 2
"""

PK_COLUMN_SETS_SQL = f"""
SELECT c.relname AS tbl,
       (SELECT array_agg(att.attname ORDER BY att.attname)
          FROM unnest(con.conkey) AS k(a)
          JOIN pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = k.a) AS cols
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
WHERE con.connamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND con.contype = 'p'
ORDER BY 1
"""

# the FULL FK identity (local codex P2: unordered local column-sets alone
# would accept a migration that references the wrong table/columns, reorders
# a positional mapping, or swaps CASCADE for RESTRICT): ordered local
# columns, referenced table, ordered referenced columns, delete action.
# WITH ORDINALITY preserves constraint-definition order.
FK_IDENTITY_SQL = f"""
SELECT c.relname AS tbl,
       (SELECT array_agg(att.attname ORDER BY k.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS k(a, ord)
          JOIN pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = k.a) AS cols,
       cf.relname AS ref_tbl,
       (SELECT array_agg(att.attname ORDER BY k.ord)
          FROM unnest(con.confkey) WITH ORDINALITY AS k(a, ord)
          JOIN pg_attribute att
            ON att.attrelid = con.confrelid AND att.attnum = k.a) AS ref_cols,
       con.confdeltype::text AS deltype
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_class cf ON cf.oid = con.confrelid
WHERE con.connamespace = 'public'::regnamespace
  AND {APP_RELKIND_FILTER}
  AND con.contype = 'f'
ORDER BY 1
"""

#: pg_constraint.confdeltype → the ondelete spelling tables.py uses
DELTYPE = {"a": None, "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}


@pytest.fixture()
def migrated(require_services: None) -> None:
    """Apply migrations (idempotent). Sync fixture: alembic's env.py drives
    its own asyncio.run, which must not happen inside a running event loop."""
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


async def _rows(conn: AsyncConnection, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in (await conn.execute(sa.text(sql))).fetchall()]


def _live_unique_sets(rows: list[tuple[Any, ...]]) -> set[tuple[str, frozenset[str]]]:
    """(table, key-column set) per usable plain unique index; rows whose join
    lost an expression member (len(cols) != nkeys) are dropped rather than
    matched against a smaller set."""
    return {
        (tbl, frozenset(cols))
        for tbl, nkeys, cols in rows
        if cols is not None and len(cols) == nkeys
    }


@pytest.mark.integration
async def test_live_catalog_holds_every_universal_property(migrated: None) -> None:
    """The same five universals, answered by the DATABASE the migrations
    actually produced."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            assert await _rows(conn, FK_WITHOUT_COVERING_INDEX_SQL) == []
            assert await _rows(conn, FK_WITH_DEFAULT_ON_DELETE_SQL) == []
            assert await _rows(conn, NAIVE_TIMESTAMP_SQL) == []
            assert await _rows(conn, TABLE_WITHOUT_PK_SQL) == []
            nullable_members = await _rows(conn, NULLABLE_UNIQUE_MEMBER_SQL)
            violations = [
                (tbl, name)
                for tbl, name, pred in nullable_members
                if not (
                    pred is not None
                    and _normalized_predicate(pred) == EXEMPT_PREDICATES.get((tbl, name))
                )
            ]
            assert violations == []
            # expression slots are invisible to the attribute join above —
            # every expression unique must be name-exempted (Codex #115 R2)
            unexempted = [
                (tbl, name)
                for tbl, name in await _rows(conn, EXPRESSION_UNIQUE_SQL)
                if (tbl, name) not in EXPRESSION_UNIQUES_OK
            ]
            assert unexempted == []
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_live_schema_matches_metadata_instance_sets(migrated: None) -> None:
    """Instance-set parity (local codex P1): the property queries above are
    vacuously green for objects a migration forgot to CREATE — this is the
    existence half. Every metadata table must exist live with the same
    columns, PK, FK column-sets, and plain unique column-sets. Superset on
    the live side is tolerated only for whole tables (alembic_version);
    within a table the sets must match exactly, both directions. This is an
    existence/NAMING parity — column TYPES are out of scope (timestamp drift
    is property-checked above; core-table types are pinned offline in
    test_core_tables_schema). FKs compare by FULL identity (ordered local
    columns → referenced table + ordered referenced columns + delete
    action), and the live table set must be EXACTLY metadata ∪
    {alembic_version} — an extra live table is drift, not tolerance."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            live_cols: dict[str, set[str]] = {}
            for tbl, name in await _rows(conn, LIVE_COLUMNS_SQL):
                live_cols.setdefault(tbl, set()).add(name)
            assert set(live_cols) == set(metadata.tables) | {"alembic_version"}
            live_pks: dict[str, set[frozenset[str]]] = {}
            for tbl, cols in await _rows(conn, PK_COLUMN_SETS_SQL):
                if cols is not None:
                    live_pks.setdefault(tbl, set()).add(frozenset(cols))
            live_fks: dict[str, set[tuple[Any, ...]]] = {}
            for tbl, cols, ref_tbl, ref_cols, deltype in await _rows(conn, FK_IDENTITY_SQL):
                if cols is None or ref_cols is None:
                    continue
                live_fks.setdefault(tbl, set()).add(
                    (tuple(cols), ref_tbl, tuple(ref_cols), DELTYPE[deltype])
                )
            live_uniques = _live_unique_sets(await _rows(conn, UNIQUE_INDEX_COLUMN_SETS_SQL))
            live_partials: dict[str, set[tuple[frozenset[str], str]]] = {}
            for tbl, nkeys, cols, pred in await _rows(conn, PARTIAL_UNIQUE_INDEX_SQL):
                if cols is None or len(cols) != nkeys:
                    continue
                live_partials.setdefault(tbl, set()).add(
                    (frozenset(cols), _normalized_predicate(pred))
                )

            for table in metadata.tables.values():
                name = table.name
                assert live_cols[name] == {c.name for c in table.columns}, name
                assert live_pks.get(name) == {frozenset(table.primary_key.columns.keys())}, name
                assert live_fks.get(name, set()) == {
                    (
                        tuple(fk.column_keys),
                        fk.referred_table.name,
                        tuple(el.column.name for el in fk.elements),
                        fk.ondelete and fk.ondelete.upper(),
                    )
                    for fk in table.foreign_key_constraints
                }, name
                assert {s for t, s in live_uniques if t == name} == (
                    _metadata_plain_unique_sets(table)
                ), name
                # partial uniques carry an identity too (columns AND scope) —
                # a dropped relations_by_signature must fail HERE, not stay
                # invisible to every query that excludes partials (codex #115)
                assert live_partials.get(name, set()) == (_metadata_partial_unique_sets(table)), (
                    name
                )
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_live_catalog_pins_the_identity_registry(migrated: None) -> None:
    """The spec-side pins, mirrored onto the live catalog: a migration that
    dropped one of the §27.3/§27.4 identity uniques would keep the offline
    half green (tables.py still declares it). Includes the review_ledger
    anti-pin: its ONLY unique is the PK, so append-history precedence
    survives."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            unique_sets = _live_unique_sets(await _rows(conn, UNIQUE_INDEX_COLUMN_SETS_SQL))
            for table_name, cols in IDENTITY_UNIQUES.items():
                assert (table_name, frozenset(cols)) in unique_sets, (table_name, cols)
            ledger_uniques = {cols for tbl, cols in unique_sets if tbl == "review_ledger"}
            assert ledger_uniques == {frozenset({"id"})}
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_catalog_queries_flag_a_planted_violator(migrated: None) -> None:
    """Probe-of-probe, in-suite: a scratch table violating every property at
    once (unindexed FK with default ON DELETE, naive timestamp, no PK,
    nullable unique member) must be FLAGGED by every query. An enumeration
    bug that returns [] fails here instead of turning the whole suite falsely
    green. The table lives only inside this never-committed transaction with
    a run-unique name (local codex P2): a killed run leaves nothing behind,
    and other connections never see it."""
    name = f"zz_h20b_probe_{uuid.uuid4().hex[:8]}"
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(
                sa.text(
                    f"""
                    CREATE TABLE {name} (
                      ref text REFERENCES projects(name),
                      seen timestamp without time zone,
                      val text,
                      CONSTRAINT {name}_unique UNIQUE (val)
                    )
                    """
                )
            )
            # a unique over an EXPRESSION of a nullable column — the shape
            # the attribute joins cannot see (Codex #115 R2)
            await conn.execute(sa.text(f"CREATE UNIQUE INDEX {name}_expr ON {name} (lower(val))"))
            # the UNIQUE on val incidentally indexes val, not ref — the FK
            # on ref stays uncovered, which is exactly the plant
            assert (name, f"{name}_ref_fkey") in set(
                await _rows(conn, FK_WITHOUT_COVERING_INDEX_SQL)
            )
            assert (name, f"{name}_ref_fkey") in set(
                await _rows(conn, FK_WITH_DEFAULT_ON_DELETE_SQL)
            )
            assert (name, "seen") in set(await _rows(conn, NAIVE_TIMESTAMP_SQL))
            assert (name, name) in set(await _rows(conn, TABLE_WITHOUT_PK_SQL))
            # pred is None: a plain (non-partial) unique over a nullable
            # member — the exact shape the exemption must NOT cover
            assert (name, "val", None) in set(await _rows(conn, NULLABLE_UNIQUE_MEMBER_SQL))
            # and the unique-set mirror sees the probe's unique
            assert (name, frozenset({"val"})) in _live_unique_sets(
                await _rows(conn, UNIQUE_INDEX_COLUMN_SETS_SQL)
            )
            # the expression-unique detector sees the lower(val) plant
            assert (name, f"{name}_expr") in set(await _rows(conn, EXPRESSION_UNIQUE_SQL))
            await conn.rollback()
    finally:
        await engine.dispose()
