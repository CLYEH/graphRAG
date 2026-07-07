"""Why: create_build's error mapping and run_build's control flow — the §5 stage
sequence, the cancel-checkpoint placement, the §22 failed-ratio abort, and the
terminal build/job status rules — are logic that must hold independently of
Postgres. These component tests drive the REAL functions with the store seams
(get_project / jobs CRUD / create_build / record_run) stubbed and a fake
engine, so the orchestration is exercised with zero I/O; the live SQL is the
integration suite's job (BA1b's component + integration split, which also keeps
this integration-only module inside the fast-suite coverage gate).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.builds import creation, orchestrator
from core.builds.creation import create_build
from core.builds.orchestrator import (
    _STAGE_ORDER,
    BuildNotResumableError,
    StageResult,
    Stages,
    run_build,
)
from core.observability.recorder import StepReport
from core.observability.spec import ItemOutcome
from core.registry import jobs as jobs_module
from core.registry.jobs import Job, JobNotFoundError
from core.registry.store import ProjectNotFoundError

pytestmark = pytest.mark.contract

_StageFn = Callable[[Any, str, uuid.UUID], Awaitable[StageResult]]


# --------------------------------------------------------------------------- #
# create_build — hermetic (fake connection, stubbed get_project)
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _Conn:
    """Minimal fake AsyncConnection: run_build only ever calls execute() (the
    terminal builds.update); create_build calls it for the returning-insert."""

    def __init__(self, *, result: Any = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.executed: list[Any] = []

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _Conn:
        return self  # doubles as its own transaction context manager

    async def execute(self, statement: Any) -> Any:
        self.executed.append(statement)
        if self._raises is not None:
            raise self._raises
        return self._result


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate


def _integrity_error(sqlstate: str) -> IntegrityError:
    return IntegrityError("INSERT", {}, _Orig(sqlstate))


async def test_create_build_returns_the_inserted_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _present(conn: Any, name: str) -> object:
        return object()

    monkeypatch.setattr(creation, "get_project", _present)
    new_id = uuid.uuid4()
    conn = _Conn(result=_Result(new_id))

    got = await create_build(cast(AsyncConnection, conn), "p", config_hash="c", source_hash="s")

    assert got == new_id
    assert len(conn.executed) == 1  # the returning-insert


async def test_create_build_absent_project_raises_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _absent(conn: Any, name: str) -> None:
        return None

    monkeypatch.setattr(creation, "get_project", _absent)
    conn = _Conn()

    with pytest.raises(ProjectNotFoundError):
        await create_build(cast(AsyncConnection, conn), "p")
    assert conn.executed == []  # no insert attempted


async def test_create_build_maps_fk_violation_to_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _present(conn: Any, name: str) -> object:
        return object()

    monkeypatch.setattr(creation, "get_project", _present)
    conn = _Conn(raises=_integrity_error("23503"))  # FK violation → project raced away

    with pytest.raises(ProjectNotFoundError):
        await create_build(cast(AsyncConnection, conn), "p")


async def test_create_build_reraises_a_non_fk_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _present(conn: Any, name: str) -> object:
        return object()

    monkeypatch.setattr(creation, "get_project", _present)
    conn = _Conn(raises=_integrity_error("23505"))  # unique violation — a real bug, not "not found"

    with pytest.raises(IntegrityError):
        await create_build(cast(AsyncConnection, conn), "p")


# --------------------------------------------------------------------------- #
# run_build — hermetic (fake engine + stubbed store seams via a spy)
# --------------------------------------------------------------------------- #

_FRESH_BUILD = uuid.uuid4()
_RUN_ID = uuid.uuid4()


def _job(project: str = "p", *, build_id: uuid.UUID | None = None) -> Job:
    return Job(
        id=uuid.uuid4(),
        project=project,
        kind="build",
        build_id=build_id,
        status="queued",
        step=None,
        progress=0.0,
        message=None,
        error=None,
        cancel_requested=False,
        created_at=datetime.now(tz=UTC),
        finished_at=None,
    )


class _Engine:
    def connect(self) -> _Conn:
        return _Conn()  # each connect() a fresh no-op connection


def _fake_engine() -> AsyncEngine:
    """The fake engine typed as AsyncEngine — run_build only touches
    connect()/begin()/execute(), all of which _Engine/_Conn stub."""
    return cast(AsyncEngine, _Engine())


class _Spy:
    """Stubs the store seams run_build calls, recording enough to assert the
    orchestration decisions without any I/O."""

    def __init__(
        self,
        job: Job | None,
        *,
        cancel_script: list[bool] | None = None,
        build_status: str | None = "building",
    ) -> None:
        self.job = job
        self.cancel_script = list(cancel_script or [])
        self._status_value = build_status
        self.progress_calls: list[dict[str, Any]] = []
        self.created = False
        self.recorded: dict[str, Any] | None = None

    async def get_job(self, conn: Any, job_id: uuid.UUID) -> Job | None:
        return self.job

    async def lock_job(self, conn: Any, job_id: uuid.UUID) -> Job | None:
        return self.job  # the locked row (same shape; carries build_id)

    async def set_progress(self, conn: Any, job_id: uuid.UUID, **kw: Any) -> None:
        self.progress_calls.append(kw)

    async def is_cancel_requested(self, conn: Any, job_id: uuid.UUID) -> bool:
        return self.cancel_script.pop(0) if self.cancel_script else False

    async def create_build(
        self,
        conn: Any,
        project: str,
        *,
        config_hash: str | None = None,
        source_hash: str | None = None,
    ) -> uuid.UUID:
        self.created = True
        return _FRESH_BUILD

    async def build_status(self, conn: Any, project: str, build_id: uuid.UUID) -> str | None:
        return self._status_value

    async def record_run(
        self,
        conn: Any,
        project: str,
        build_id: uuid.UUID,
        kind: str,
        steps: list[StepReport],
        *,
        error: str | None = None,
        cancelled: bool = False,
    ) -> uuid.UUID:
        self.recorded = {"steps": steps, "error": error, "cancelled": cancelled, "kind": kind}
        return _RUN_ID


def _install(monkeypatch: pytest.MonkeyPatch, spy: _Spy) -> None:
    monkeypatch.setattr(jobs_module, "get_job", spy.get_job)
    monkeypatch.setattr(jobs_module, "lock_job", spy.lock_job)
    monkeypatch.setattr(jobs_module, "set_progress", spy.set_progress)
    monkeypatch.setattr(jobs_module, "is_cancel_requested", spy.is_cancel_requested)
    monkeypatch.setattr(orchestrator, "create_build", spy.create_build)
    monkeypatch.setattr(orchestrator, "record_run", spy.record_run)
    monkeypatch.setattr(orchestrator, "_build_status", spy.build_status)


def _stage(name: str, calls: list[str], **over: Any) -> _StageFn:
    outcomes: tuple[ItemOutcome, ...] = over.get("outcomes", ())
    exc: Exception | None = over.get("exc")

    async def fn(conn: Any, project: str, build_id: uuid.UUID) -> StageResult:
        calls.append(name)
        if exc is not None:
            raise exc
        return StageResult(outcomes=outcomes)

    return fn


def _stages(calls: list[str], **overrides: _StageFn) -> Stages:
    return Stages(**{n: overrides.get(n) or _stage(n, calls) for n in _STAGE_ORDER})


async def test_happy_path_creates_runs_all_stages_and_marks_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _Spy(_job())
    _install(monkeypatch, spy)
    calls: list[str] = []

    # no step_failure_ratio → exercises the _default_threshold() settings read
    outcome = await run_build(_fake_engine(), "p", uuid.uuid4(), _stages(calls))

    assert outcome.status == "ready"
    assert not outcome.cancelled
    assert outcome.error is None
    assert outcome.build_id == _FRESH_BUILD
    assert outcome.run_id == _RUN_ID
    assert spy.created  # fresh build minted
    assert calls == list(_STAGE_ORDER)  # §5 order
    # progress: running → one per stage → done, with progress reaching 1.0
    statuses = [c["status"] for c in spy.progress_calls if "status" in c]
    assert statuses[0] == "running" and statuses[-1] == "done"
    assert any(c.get("progress") == 1.0 for c in spy.progress_calls)
    assert spy.recorded is not None
    assert spy.recorded["cancelled"] is False and spy.recorded["error"] is None
    assert [s.step_name for s in spy.recorded["steps"]] == list(_STAGE_ORDER)


async def test_stage_crash_fails_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(_job())
    _install(monkeypatch, spy)
    calls: list[str] = []
    boom = _stage("graph", calls, exc=RuntimeError("kaboom"))

    outcome = await run_build(_fake_engine(), "p", uuid.uuid4(), _stages(calls, graph=boom))

    assert outcome.status == "failed"
    assert outcome.error is not None and "graph:" in outcome.error
    assert calls == ["ingest", "clean", "graph"]
    assert spy.recorded is not None and spy.recorded["error"] is not None
    assert [s.step_name for s in spy.recorded["steps"]] == ["ingest", "clean"]
    # the last job update carries the failure status + the full §15 Error shape
    last = spy.progress_calls[-1]
    assert last["status"] == "failed"
    assert last["error"] == {"code": "INTERNAL", "message": outcome.error, "details": None}


async def test_failure_ratio_over_threshold_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(_job())
    _install(monkeypatch, spy)
    calls: list[str] = []
    flaky = _stage(
        "clean",
        calls,
        outcomes=(
            ItemOutcome("document", "a", "failed"),
            ItemOutcome("document", "b", "failed"),
            ItemOutcome("document", "c", "skipped"),
        ),
    )

    outcome = await run_build(
        _fake_engine(), "p", uuid.uuid4(), _stages(calls, clean=flaky), step_failure_ratio=0.5
    )

    assert outcome.status == "failed"
    assert outcome.error is not None and "§22" in outcome.error
    assert calls == ["ingest", "clean"]


async def test_failure_ratio_under_threshold_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(_job())
    _install(monkeypatch, spy)
    calls: list[str] = []
    tolerated = _stage(
        "graph",
        calls,
        outcomes=(ItemOutcome("document", "a", "failed"), ItemOutcome("document", "b", "skipped")),
    )

    outcome = await run_build(
        _fake_engine(), "p", uuid.uuid4(), _stages(calls, graph=tolerated), step_failure_ratio=0.5
    )

    assert outcome.status == "ready"
    assert calls == list(_STAGE_ORDER)


async def test_cancel_between_stages_stops_and_records_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # checkpoint before ingest → False (run it), before clean → True (stop)
    spy = _Spy(_job(), cancel_script=[False, True])
    _install(monkeypatch, spy)
    calls: list[str] = []

    outcome = await run_build(_fake_engine(), "p", uuid.uuid4(), _stages(calls))

    assert outcome.cancelled
    assert outcome.status == "failed"  # cancelled reuses builds.status='failed'
    assert outcome.error is None
    assert calls == ["ingest"]
    assert spy.recorded is not None and spy.recorded["cancelled"] is True
    assert spy.progress_calls[-1]["status"] == "cancelled"


async def test_cancel_during_last_stage_is_honored_before_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # False for all six between-stages checkpoints, True on the final recheck —
    # a cancel that lands while summarize runs has no next checkpoint, so the
    # post-loop recheck is the only thing that can catch it
    spy = _Spy(_job(), cancel_script=[False] * 6 + [True])
    _install(monkeypatch, spy)
    calls: list[str] = []

    outcome = await run_build(_fake_engine(), "p", uuid.uuid4(), _stages(calls))

    assert calls == list(_STAGE_ORDER)  # every stage ran to completion
    assert outcome.cancelled  # ...but the late cancel is still honored
    assert outcome.status == "failed"
    assert spy.recorded is not None and spy.recorded["cancelled"] is True


async def test_cancel_before_first_stage_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(_job(), cancel_script=[True])
    _install(monkeypatch, spy)
    calls: list[str] = []

    outcome = await run_build(_fake_engine(), "p", uuid.uuid4(), _stages(calls))

    assert outcome.cancelled and calls == []
    assert spy.recorded is not None and spy.recorded["steps"] == []


async def test_resume_a_building_build_skips_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = uuid.uuid4()
    spy = _Spy(_job(), build_status="building")
    _install(monkeypatch, spy)
    calls: list[str] = []

    outcome = await run_build(_fake_engine(), "p", uuid.uuid4(), _stages(calls), build_id=existing)

    assert outcome.build_id == existing
    assert outcome.status == "ready"
    assert not spy.created  # resumed, no fresh build


async def test_resume_a_finished_build_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(_job(), build_status="ready")  # not 'building'
    _install(monkeypatch, spy)
    calls: list[str] = []

    with pytest.raises(BuildNotResumableError):
        await run_build(_fake_engine(), "p", uuid.uuid4(), _stages(calls), build_id=uuid.uuid4())
    assert calls == []


async def test_unknown_job_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(None)
    _install(monkeypatch, spy)

    with pytest.raises(JobNotFoundError):
        await run_build(_fake_engine(), "p", uuid.uuid4(), _stages([]))


async def test_job_from_another_project_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(_job(project="other"))
    _install(monkeypatch, spy)

    with pytest.raises(ValueError, match="belongs to project"):
        await run_build(_fake_engine(), "p", uuid.uuid4(), _stages([]))
