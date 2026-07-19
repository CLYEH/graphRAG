"""Why: the recorder is §18's write path — verbosity decides which item ROWS
survive while counters must stay complete (the §27.7 retry boundary reads
the failed rows verbatim, so the frozen minimum can never be filtered away),
and an unknown verbosity must fall back to the SAFE minimum, not silently
widen. The unit half pins the filter; the live half proves the three-layer
write and retention purge."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.observability.recorder import (
    StepReport,
    _persistable,
    purge_expired_items,
    record_run,
)
from core.observability.spec import ItemOutcome
from core.stores import tables
from tests.conftest import ensure_project

REPO_ROOT = Path(__file__).resolve().parent.parent


def _outcomes() -> tuple[ItemOutcome, ...]:
    out: list[ItemOutcome] = []
    for i in range(23):
        out.append(ItemOutcome("document", f"hash-{i}", "indexed"))
    out.append(ItemOutcome("document", "hash-bad", "failed"))
    out.append(ItemOutcome("entity", "key-skip", "skipped"))
    return tuple(out)


def test_failures_mode_keeps_exactly_the_retry_boundary_input() -> None:
    kept = _persistable(_outcomes(), "failures")
    assert [(o.item_ref, o.status) for o in kept] == [
        ("hash-bad", "failed"),
        ("key-skip", "skipped"),
    ]


def test_sampled_mode_adds_every_tenth_success() -> None:
    kept = _persistable(_outcomes(), "sampled")
    successes = [o for o in kept if o.status == "indexed"]
    assert [o.item_ref for o in successes] == ["hash-0", "hash-10", "hash-20"]
    assert sum(1 for o in kept if o.status == "failed") == 1  # failures always kept


def test_all_mode_keeps_everything() -> None:
    assert len(_persistable(_outcomes(), "all")) == 25


def test_duplicate_item_refs_dedupe_first_kept() -> None:
    """§27.7's own dedup rule at the write path: the table's unique index
    (step_id, item_kind, item_ref) would roll the WHOLE run back on a
    duplicate row — reachable under default verbosity when ingest emits one
    skipped outcome per duplicate payload."""
    outcomes = (
        ItemOutcome("document", "hash-dup", "skipped"),
        ItemOutcome("document", "hash-dup", "skipped"),
        ItemOutcome("document", "hash-dup", "failed"),  # FAILED must dominate
        ItemOutcome("entity", "hash-dup", "failed"),  # different kind — kept
    )
    kept = _persistable(outcomes, "failures")
    # failed dominates at the FIRST-seen position (Codex round 7): the §27.7
    # retry boundary reads persisted failed rows, so a failed occurrence must
    # never be masked by an earlier skipped/success for the same ref
    assert [(o.item_kind, o.item_ref, o.status) for o in kept] == [
        ("document", "hash-dup", "failed"),
        ("entity", "hash-dup", "failed"),
    ]
    assert len(_persistable(outcomes, "all")) == 2  # dedup applies in every mode


def test_retry_skip_step_names_match_the_orchestrator_stages() -> None:
    """RB1-retry-skip filters ``pipeline_steps`` on ``reads.GRAPH_STEP_NAME`` (the
    failed-set reader) and ``reads.RESOLVE_STEP_NAME`` (the resolve-ran guard). Both
    MUST equal the orchestrator's §5 stage names, and RESOLVE must come AFTER GRAPH
    — the guard's whole premise is "resolve runs after graph, so its presence means
    a post-resolve graph layer". A rename of either stage that didn't update the
    constant would silently break the guard (read zero failures → graph-less retry;
    or miss a post-resolve parent → drop merged audit rows) — the class of silent
    break this lockstep exists to catch."""
    from core.builds.orchestrator import _STAGE_ORDER
    from core.observability.reads import GRAPH_STEP_NAME, RESOLVE_STEP_NAME

    assert GRAPH_STEP_NAME in _STAGE_ORDER and RESOLVE_STEP_NAME in _STAGE_ORDER
    assert _STAGE_ORDER.index(RESOLVE_STEP_NAME) == _STAGE_ORDER.index(GRAPH_STEP_NAME) + 1


def test_dedupe_keeps_first_when_no_failure() -> None:
    """Without a failed occurrence the first-seen row wins (deterministic)."""
    outcomes = (
        ItemOutcome("document", "d", "indexed"),
        ItemOutcome("document", "d", "skipped"),
    )
    kept = _persistable(outcomes, "all")
    assert [(o.status) for o in kept] == ["indexed"]


async def test_unknown_verbosity_falls_back_to_the_frozen_minimum() -> None:
    """A typo'd config must not widen (or lose) the persisted set — the
    §27.7 retry input is exactly the failures rows."""
    kept = _persistable(_outcomes(), "everything-please")
    assert all(o.status in ("failed", "skipped") for o in kept)


async def test_misattributed_build_ids_are_refused() -> None:
    """pipeline_runs has NO FK (Codex round 5): a cross-project or pruned
    build_id would write observability rows under the wrong build — the
    binding is verified inside the write txn (FOR SHARE), refused loud."""
    import uuid as _uuid
    from types import SimpleNamespace
    from typing import Any, cast

    class _Txn:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Conn:
        def __init__(self, owner: str | None) -> None:
            self._owner = owner

        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Txn:
            return _Txn()

        async def execute(self, statement: Any, rows: Any = None) -> Any:
            return SimpleNamespace(scalar_one_or_none=lambda: self._owner)

    with pytest.raises(LookupError, match="does not exist"):
        await record_run(cast(Any, _Conn(None)), "p", _uuid.uuid4(), "ingest", [])
    with pytest.raises(LookupError, match="belongs to project"):
        await record_run(cast(Any, _Conn("other")), "p", _uuid.uuid4(), "ingest", [])


async def test_dirty_connections_are_refused_loaned_clean() -> None:
    """The C6b idiom (Codex round 2): a rollback here would silently destroy
    the CALLER's uncommitted pipeline writes — refuse the dirty connection
    loud instead."""
    from typing import Any, cast

    class _Dirty:
        def in_transaction(self) -> bool:
            return True

    with pytest.raises(RuntimeError, match="no open transaction"):
        await record_run(cast(Any, _Dirty()), "p", uuid.uuid4(), "ingest", [])
    with pytest.raises(RuntimeError, match="no open transaction"):
        await purge_expired_items(cast(Any, _Dirty()))


async def test_default_verbosity_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round 2: the 🔧 tunable must WORK without every caller wiring
    it — verbosity=None reads observability_item_logging from settings; an
    explicit argument overrides."""
    import uuid as _uuid
    from types import SimpleNamespace
    from typing import Any, cast

    captured_rows: list[Any] = []

    class _Txn:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Conn:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Txn:
            return _Txn()

        async def execute(self, statement: Any, rows: Any = None) -> Any:
            if not hasattr(statement, "table"):  # the build-binding SELECT
                return SimpleNamespace(scalar_one_or_none=lambda: "p")
            if rows is not None:
                captured_rows.extend(rows)
            return SimpleNamespace(scalar_one=lambda: _uuid.uuid4(), rowcount=1)

    import core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(observability_item_logging="all"),
    )
    await record_run(
        cast(Any, _Conn()),
        "p",
        _uuid.uuid4(),
        "ingest",
        [StepReport("chunk", (ItemOutcome("document", "hash-ok", "indexed"),))],
    )
    assert len(captured_rows) == 1  # "all" persisted the success row


async def test_purge_refuses_a_zero_window() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    clean = SimpleNamespace(in_transaction=lambda: False)
    with pytest.raises(ValueError, match="retention_days must be >= 1"):
        await purge_expired_items(cast(Any, clean), retention_days=0)


# ---------------------------------------------------------- integration ----


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def project(migrated: None) -> AsyncIterator[str]:
    name = f"obs-{uuid.uuid4().hex[:10]}"
    yield name
    engine = _engine()
    async with engine.connect() as conn:
        await conn.execute(
            tables.pipeline_runs.delete().where(tables.pipeline_runs.c.project == name)
        )
        await conn.execute(tables.builds.delete().where(tables.builds.c.project == name))
        await conn.commit()
    await engine.dispose()


@pytest.mark.integration
async def test_record_run_persists_three_layers_with_verbosity(project: str) -> None:
    """§18 end to end: run → steps (complete counters) → items (filtered
    rows); a failed item marks step AND run failed — the Console line's
    exact inputs."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            build_id: uuid.UUID = (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="building")
                    .returning(tables.builds.c.id)
                )
            ).scalar_one()
            await conn.commit()
            run_id = await record_run(
                conn,
                project,
                build_id,
                "ingest",
                [StepReport("chunk", _outcomes())],
                verbosity="failures",
            )
            run = (
                await conn.execute(
                    sa.select(tables.pipeline_runs).where(tables.pipeline_runs.c.id == run_id)
                )
            ).one()
            assert run.status == "failed"  # one failed item fails the run
            step = (
                await conn.execute(
                    sa.select(tables.pipeline_steps).where(tables.pipeline_steps.c.run_id == run_id)
                )
            ).one()
            # counters COMPLETE regardless of verbosity
            assert (step.input_count, step.output_count, step.skipped_count, step.failed_count) == (
                25,
                23,
                1,
                1,
            )
            items = (
                await conn.execute(
                    sa.select(tables.pipeline_step_items).where(
                        tables.pipeline_step_items.c.step_id == step.id
                    )
                )
            ).fetchall()
            assert sorted((i.item_ref, i.status) for i in items) == [
                ("hash-bad", "failed"),
                ("key-skip", "skipped"),
            ]

            # §27.7 build binding: a non-validation kind with NULL build_id
            # is refused by the CHECK, loud. (The reads above auto-began a
            # txn — ending it is the CALLER's job under the loaned-clean
            # contract, exactly what the fence enforces.)
            await conn.rollback()
            with pytest.raises(Exception, match="pipeline_runs_build_binding"):
                await record_run(conn, project, None, "ingest", [])
            await conn.rollback()

            # the SUCCESS path must satisfy the frozen JobStatus CHECK
            # (queued/running/done/failed/cancelled) — the cell whose absence
            # let "succeeded" slip past every masked test (local blocker)
            await conn.rollback()  # loaned-clean: end this test's read txn
            clean_run = await record_run(
                conn,
                project,
                build_id,
                "ingest",
                [StepReport("chunk", (ItemOutcome("document", "hash-ok", "indexed"),))],
            )
            stored = (
                await conn.execute(
                    sa.select(tables.pipeline_runs.c.status).where(
                        tables.pipeline_runs.c.id == clean_run
                    )
                )
            ).scalar_one()
            assert stored == "done"

            # retention: nothing young enough is purged (loaned-clean: end
            # the read txn our assertions auto-began first)
            await conn.rollback()
            assert await purge_expired_items(conn, retention_days=30) == 0
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_failed_override_rolls_run_status_to_the_caller_outcome(project: str) -> None:
    """A build run's status rolls up to the caller's BUILD outcome, not raw
    item-failure: §22 tolerates under-threshold item failures, so a ready
    build's run must read 'done' though a step recorded a failed item — a
    'failed' run on a ready build would mislead §18 Health. ``failed=`` overrides
    the inference both ways; the failed item's STEP still reads 'failed' (detail
    preserved)."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            build_id: uuid.UUID = (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="building")
                    .returning(tables.builds.c.id)
                )
            ).scalar_one()
            await conn.commit()

            # a failed item, but the caller (under §22 threshold) says not failed
            tolerated = await record_run(
                conn,
                project,
                build_id,
                "ingest",
                [StepReport("chunk", (ItemOutcome("document", "bad", "failed"),))],
                failed=False,
            )
            # a clean run the caller forces failed (e.g. a §22 abort with no item rows)
            forced = await record_run(
                conn,
                project,
                build_id,
                "ingest",
                [StepReport("chunk", (ItemOutcome("document", "ok", "indexed"),))],
                failed=True,
            )
            statuses = {
                row.id: row.status
                for row in (
                    await conn.execute(
                        sa.select(tables.pipeline_runs.c.id, tables.pipeline_runs.c.status).where(
                            tables.pipeline_runs.c.id.in_([tolerated, forced])
                        )
                    )
                ).all()
            }
            assert statuses[tolerated] == "done"  # override suppressed the failed-item inference
            assert statuses[forced] == "failed"  # override forced failed on a clean run
            step_status = (
                await conn.execute(
                    sa.select(tables.pipeline_steps.c.status).where(
                        tables.pipeline_steps.c.run_id == tolerated
                    )
                )
            ).scalar_one()
            assert step_status == "failed"  # the item failure is still recorded at the step
            await conn.rollback()
    finally:
        await engine.dispose()


def test_contract_status_map_is_lockstep_with_the_lights() -> None:
    """A sixth light without a contract mapping would KeyError at report
    time — the map must stay total over STATUS_LIGHTS (reviewer nit)."""
    from core.observability.health import _CONTRACT_STATUS, STATUS_LIGHTS

    assert set(_CONTRACT_STATUS) == set(STATUS_LIGHTS)


def test_status_light_precedence_is_total() -> None:
    """§19: one light, most actionable wins — every combination resolves in
    the documented order (a judge surface's decision table, pinned whole)."""
    from itertools import product

    from core.observability.health import status_light

    for failed, drift, regressed, pending in product((False, True), repeat=4):
        light = status_light(
            newest_failed=failed,
            drift=drift,
            eval_regressed=regressed,
            pending_review=pending,
        )
        if failed:
            assert light == "Build failed"
        elif drift:
            assert light == "Index drift"
        elif regressed:
            assert light == "Eval regression"
        elif pending:
            assert light == "Needs review"
        else:
            assert light == "Healthy"


async def test_record_run_shapes_on_fakes() -> None:
    """The write path on a fake conn: one txn, run→steps→items in order,
    complete counters, filtered rows — the seams the fast gate guards."""
    import uuid as _uuid
    from types import SimpleNamespace
    from typing import Any, cast

    inserted: list[tuple[str, Any]] = []

    class _Txn:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Conn:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Txn:
            return _Txn()

        async def execute(self, statement: Any, rows: Any = None) -> Any:
            if not hasattr(statement, "table"):  # the build-binding SELECT
                return SimpleNamespace(scalar_one_or_none=lambda: "p")
            table = statement.table.name
            inserted.append((table, rows))
            return SimpleNamespace(scalar_one=lambda: _uuid.uuid4(), rowcount=1)

    run_id = await record_run(
        cast(Any, _Conn()),
        "p",
        _uuid.uuid4(),
        "ingest",
        [StepReport("chunk", _outcomes())],
        verbosity="failures",
    )
    assert run_id is not None
    tables_written = [t for t, _ in inserted]
    assert tables_written == ["pipeline_runs", "pipeline_steps", "pipeline_step_items"]
    item_rows = inserted[2][1]
    assert [(r["item_ref"], r["status"]) for r in item_rows] == [
        ("hash-bad", "failed"),
        ("key-skip", "skipped"),
    ]


async def test_purge_composes_the_retention_cutoff_on_fakes() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    executed: list[str] = []

    class _Txn:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Conn:
        def in_transaction(self) -> bool:
            return False

        def begin(self) -> _Txn:
            return _Txn()

        async def execute(self, statement: Any) -> Any:
            executed.append(str(statement))
            return SimpleNamespace(rowcount=7)

    deleted = await purge_expired_items(cast(Any, _Conn()), retention_days=30)
    assert deleted == 7
    assert "pipeline_step_items" in executed[0] and "make_interval" in executed[0]


async def test_eval_regressed_cells_on_fakes() -> None:
    """§20's light logic without stores: no ready build → dark; comparable
    regressing → lit; mismatched fingerprint → dark (measured facts only)."""
    import uuid as _uuid
    from types import SimpleNamespace
    from typing import Any, cast

    from core.observability.health import HealthReport, _eval_regressed

    class _Conn:
        def __init__(self, ready_eval: Any, active_eval: Any) -> None:
            self._ready = ready_eval
            self._active = active_eval

        async def execute(self, statement: Any) -> Any:
            sql = str(statement)
            if "'ready'" in sql or "= :status_1" in sql and "builds.id," in sql:
                row = (
                    None
                    if self._ready is None
                    else SimpleNamespace(id=_uuid.uuid4(), eval=self._ready)
                )
                return SimpleNamespace(one_or_none=lambda: row)
            return SimpleNamespace(scalar_one_or_none=lambda: self._active)

    active_id = _uuid.uuid4()
    # no ready build → dark
    assert not await _eval_regressed(cast(Any, _Conn(None, None)), "p", active_id)
    # regressing, same fingerprint → lit
    assert await _eval_regressed(
        cast(Any, _Conn({"score": 0.5, "fingerprint": "fp"}, {"score": 0.9, "fingerprint": "fp"})),
        "p",
        active_id,
    )
    # regressing but DIFFERENT fingerprint → dark (incomparable)
    assert not await _eval_regressed(
        cast(Any, _Conn({"score": 0.5, "fingerprint": "a"}, {"score": 0.9, "fingerprint": "b"})),
        "p",
        active_id,
    )
    # unscored active → dark
    assert not await _eval_regressed(
        cast(Any, _Conn({"score": 0.5, "fingerprint": "fp"}, None)), "p", active_id
    )

    # the payload speaks the FROZEN contract: lower-snake HealthStatus,
    # drift object-or-null, integer counts split out
    payload = HealthReport(
        project="p",
        status="Index drift",
        active_build_id=None,
        drift=("graph drift: 1 vs 0",),
        metrics={"pending_review": 2, "entities": 5, "eval": {"score": 0.9}},
    ).to_payload()
    assert payload["status"] == "index_drift"
    assert payload["drift"] == {"failures": ["graph drift: 1 vs 0"]}
    assert payload["pending_review"] == 2
    assert payload["counts"] == {"entities": 5}
    healthy = HealthReport(
        project="p", status="Healthy", active_build_id=None, drift=(), metrics={}
    ).to_payload()
    assert healthy["status"] == "healthy" and healthy["drift"] is None


async def test_drift_probe_degrades_and_failed_build_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round 6: /health must answer from Postgres alone when the
    newest build failed (no store probe at all), and a raising probe on the
    healthy path degrades to a STORE_UNAVAILABLE warning, never a 500."""
    import uuid as _uuid
    from typing import Any, cast

    from neo4j.exceptions import ServiceUnavailable

    from core.observability import health as health_module
    from core.observability.health import health_report

    active_id, failed_id = _uuid.uuid4(), _uuid.uuid4()

    def _rows(build_rows: list[Any]) -> Any:
        class _Result:
            def __iter__(self) -> Any:
                return iter(build_rows)

            def scalar_one(self) -> int:
                return 0

            def scalar_one_or_none(self) -> Any:
                return None

            def one_or_none(self) -> Any:
                return None

        return _Result()

    class _Conn:
        def __init__(self, build_rows: list[Any]) -> None:
            self._build_rows = build_rows
            self.calls = 0

        async def execute(self, statement: Any) -> Any:
            self.calls += 1
            return _rows(self._build_rows if self.calls == 1 else [])

    def _build(bid: Any, status: str) -> Any:
        return (bid, status, None, None, None, "p", None, None, None, None)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ServiceUnavailable("neo4j down")

    monkeypatch.setattr(health_module, "drift_failures", _boom)

    async def _must_not_acquire() -> Any:
        raise AssertionError("providers must not be acquired when the probe is skipped")

    class _Session:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Driver:
        def session(self) -> _Session:
            return _Session()

    async def _driver() -> Any:
        return _Driver()

    async def _qdrant() -> Any:
        return None

    # newest failed + active exists: the probe is SKIPPED entirely — and the
    # providers are never even ACQUIRED (Codex #62: store config must not be
    # touched on a path that measures nothing); raising ones prove it
    report = await health_report(
        cast(Any, _Conn([_build(failed_id, "failed"), _build(active_id, "active")])),
        "p",
        vector_provider=_must_not_acquire,
        graph_provider=_must_not_acquire,
    )
    assert report.status == "Build failed"
    assert report.warnings == ()

    # healthy path with the store down: degraded warning, light honest
    report = await health_report(
        cast(Any, _Conn([_build(active_id, "active")])),
        "p",
        vector_provider=_qdrant,
        graph_provider=_driver,
    )
    assert report.status == "Healthy"
    assert report.warnings and "drift check unavailable" in report.warnings[0]
    payload = report.to_payload()
    assert payload["warnings"] == [{"code": "STORE_UNAVAILABLE", "message": report.warnings[0]}]


async def test_health_report_shape_without_an_active_build_on_fakes() -> None:
    """No builds at all: Healthy, workflow metrics only (content metrics are
    active-scoped and absent), drift skipped — the report never guesses."""
    from typing import Any, cast

    from core.observability.health import health_report

    class _Rows:
        def __iter__(self) -> Any:
            return iter(())

        def scalar_one(self) -> int:
            return 0

    class _Conn:
        async def execute(self, statement: Any) -> Any:
            return _Rows()

    async def _must_not_acquire() -> Any:
        raise AssertionError("bootstrap must not acquire projection stores")

    report = await health_report(
        cast(Any, _Conn()),
        "p",
        vector_provider=_must_not_acquire,
        graph_provider=_must_not_acquire,
    )
    assert report.status == "Healthy"
    assert report.active_build_id is None and report.drift == ()
    assert report.metrics["builds_total"] == 0
    assert "entities" not in report.metrics  # content metrics are active-scoped
