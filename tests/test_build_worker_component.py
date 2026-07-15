"""Why: the worker is pure wiring — enter the job's execution lease, read its
pinned config, build the six stages off the long-lived dep bundle, and run the
build; plus the startup/shutdown lifecycle, the enqueue helpers, and the reaper
cron. These component tests spy every dependency (no Redis/Postgres/Qdrant/
Neo4j/LLM) so the lease→preflight→stages→build flow (incl. its ORDER — the lease
must bracket the whole dispatch), the lifecycle, and the dedup semantics are
pinned in the fast lane, where the real-worker integration test can't run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from arq.connections import RedisSettings
from arq.cron import CronJob

from api.workers import build_worker as bw
from core.config import get_settings


class _FakeEngine:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        yield SimpleNamespace()

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[Any]:
        yield SimpleNamespace()


class _FakeNeo4j:
    @asynccontextmanager
    async def session(self) -> AsyncIterator[str]:
        yield "SESSION"


def _ctx() -> dict[str, Any]:
    return {
        "engine": _FakeEngine(),
        "neo4j": _FakeNeo4j(),
        "qdrant": "QD",
        "embedder": "EMB",
        "llm": "LLM",
        "owner": "worker-abc",
    }


async def _capture_passthrough(conn: Any, job_id: uuid.UUID, live: Any) -> Any:
    # first-dispatch behaviour: the snapshot == the live config it captures.
    return live


def _fake_lease(calls: dict[str, Any] | None = None, *, acquired: bool = True) -> Any:
    # stand-in for job_lease: records (job_id, owner) and yields `acquired`.
    @asynccontextmanager
    async def lease(engine: Any, job_id: uuid.UUID, owner: str) -> AsyncIterator[bool]:
        if calls is not None:
            calls["lease"] = (job_id, owner)
        yield acquired

    return lease


async def test_run_build_task_wires_config_deps_and_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}
    proj = SimpleNamespace(config={"chunking": {"max_chars": 100}})

    async def _get_project(conn: Any, name: str) -> Any:
        calls["project"] = name
        return proj

    def _load_config(raw: Any) -> str:
        calls["load"] = raw
        return "CONFIG"

    def _default_stages(config: Any, **deps: Any) -> str:
        calls["stages"] = (config, deps)
        return "STAGES"

    async def _run_build(engine: Any, project: str, job_id: uuid.UUID, stages: Any) -> Any:
        calls["run"] = (project, job_id, stages)
        return SimpleNamespace(status="ready")

    async def _capture(conn: Any, job_id: uuid.UUID, live: Any) -> Any:
        calls["capture"] = (job_id, live)
        return live

    monkeypatch.setattr(bw, "job_lease", _fake_lease(calls))
    monkeypatch.setattr(bw, "get_project", _get_project)
    monkeypatch.setattr(bw, "capture_config_snapshot", _capture)
    monkeypatch.setattr(bw, "load_build_config", _load_config)
    monkeypatch.setattr(bw, "default_stages", _default_stages)
    monkeypatch.setattr(bw, "run_build", _run_build)

    jid = uuid.uuid4()
    result = await bw.run_build_task(_ctx(), "proj", str(jid))

    assert result == "ready"
    assert calls["lease"] == (jid, "worker-abc")  # the whole dispatch ran leased
    assert calls["project"] == "proj"
    # config is pinned to the job (first dispatch) then loaded from that snapshot
    assert calls["capture"] == (jid, proj.config)
    assert calls["load"] == proj.config
    config, deps = calls["stages"]
    assert config == "CONFIG"
    # the config's stages get the ctx deps: chat_model/embedder/vector_client and
    # a per-job neo4j session opened off the shared driver.
    assert deps == {
        "chat_model": "LLM",
        "embedder": "EMB",
        "vector_client": "QD",
        "graph_session": "SESSION",
    }
    assert calls["run"] == ("proj", jid, "STAGES")


async def test_run_build_task_returns_none_when_lease_held(monkeypatch: pytest.MonkeyPatch) -> None:
    preflighted: list[str] = []

    async def _get_project(conn: Any, name: str) -> Any:
        preflighted.append(name)
        return SimpleNamespace(config={})

    monkeypatch.setattr(bw, "job_lease", _fake_lease(acquired=False))
    monkeypatch.setattr(bw, "get_project", _get_project)

    result = await bw.run_build_task(_ctx(), "proj", str(uuid.uuid4()))

    assert result is None  # the no-op dispatch surfaces as a None result
    assert preflighted == []  # a lease-less dispatch does NO work — not even preflight


async def test_run_build_task_acquires_the_lease_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: the lease is the reaper's crash marker for the ENTIRE dispatch. If
    # preflight ran before acquisition, a worker dying in preflight would leave a
    # queued row with no lease — invisible to find_reapable_jobs and stranded for
    # arq's 24h timeout (the exact gap Codex flagged). Pin the ordering.
    order: list[str] = []

    @asynccontextmanager
    async def _lease(engine: Any, job_id: uuid.UUID, owner: str) -> AsyncIterator[bool]:
        order.append("lease")
        yield True

    async def _get_project(conn: Any, name: str) -> Any:
        order.append("preflight")
        return SimpleNamespace(config={})

    async def _run_build(*a: Any, **k: Any) -> Any:
        order.append("build")
        return SimpleNamespace(status="ready")

    monkeypatch.setattr(bw, "job_lease", _lease)
    monkeypatch.setattr(bw, "get_project", _get_project)
    monkeypatch.setattr(bw, "capture_config_snapshot", _capture_passthrough)
    monkeypatch.setattr(bw, "load_build_config", lambda raw: "CONFIG")
    monkeypatch.setattr(bw, "default_stages", lambda config, **k: "STAGES")
    monkeypatch.setattr(bw, "run_build", _run_build)

    await bw.run_build_task(_ctx(), "proj", str(uuid.uuid4()))

    assert order == ["lease", "preflight", "build"]


async def test_run_build_task_marks_job_failed_on_missing_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a preflight failure (vanished project) happens before run_build can mark the
    # job — the durable jobs row must be set failed, not left queued (which would
    # block project delete and mislead GET /jobs).
    marked: dict[str, Any] = {}

    async def _get_project(conn: Any, name: str) -> Any:
        return None

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        marked["job"] = job_id
        marked["fields"] = fields

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "get_project", _get_project)
    monkeypatch.setattr(bw, "set_progress", _set_progress)

    jid = uuid.uuid4()
    result = await bw.run_build_task(
        {"engine": _FakeEngine(), "owner": "worker-abc"}, "gone", str(jid)
    )

    assert result == "failed"  # terminal, not a raised exception left un-recorded
    assert marked["job"] == jid
    assert marked["fields"]["status"] == "failed"
    assert "does not exist" in marked["fields"]["error"]["message"]


async def test_run_build_task_marks_job_failed_on_malformed_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a config that fails validation raises in preflight (before the orchestrator);
    # same durable-row requirement as a missing project.
    from core.builds.config import BuildConfigError

    marked: dict[str, Any] = {}

    async def _get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(config={"resolution": "not-a-block"})

    def _load_config(raw: Any) -> Any:
        raise BuildConfigError("resolution must be a mapping")

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        marked["fields"] = fields

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "get_project", _get_project)
    monkeypatch.setattr(bw, "capture_config_snapshot", _capture_passthrough)
    monkeypatch.setattr(bw, "load_build_config", _load_config)
    monkeypatch.setattr(bw, "set_progress", _set_progress)

    result = await bw.run_build_task(_ctx(), "proj", str(uuid.uuid4()))

    assert result == "failed"
    assert marked["fields"]["status"] == "failed"
    assert "resolution must be a mapping" in marked["fields"]["error"]["message"]


async def test_run_build_task_noops_when_build_already_terminalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: a benign recovery race — a re-dispatch (arq retry / BA2d-3 reaper)
    # acquires the lease AFTER the original (starved-not-dead) worker finished the
    # build and released it, so run_build raises BuildNotResumableError (the build
    # is already terminal). The task must treat that as a no-op (return None), NOT a
    # failure — else the reaper manufactures failed/retried arq jobs for a build that
    # already succeeded.
    from core.builds.orchestrator import BuildNotResumableError

    async def _get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(config={})

    async def _run_build(*a: Any, **k: Any) -> Any:
        raise BuildNotResumableError("proj", uuid.uuid4(), "ready")

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "get_project", _get_project)
    monkeypatch.setattr(bw, "capture_config_snapshot", _capture_passthrough)
    monkeypatch.setattr(bw, "load_build_config", lambda raw: "CONFIG")
    monkeypatch.setattr(bw, "default_stages", lambda config, **k: "STAGES")
    monkeypatch.setattr(bw, "run_build", _run_build)

    result = await bw.run_build_task(_ctx(), "proj", str(uuid.uuid4()))

    assert result is None  # benign no-op, not "failed"


async def test_run_build_task_reuses_pinned_config_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: a re-dispatch (arq retry / BA2d-3 reaper) must build from the config the
    # build STARTED with, not the live project config — a mid-build PATCH /projects
    # must not drift a resuming build's chunking/ontology params. capture_config_
    # snapshot returns the pinned snapshot (C1) even though proj.config has since
    # drifted to C2, and the stages must be built from C1.
    loaded: dict[str, Any] = {}
    pinned = {"chunking": {"max_chars": 100}}  # C1 — what the build started with
    drifted = {"chunking": {"max_chars": 999}}  # C2 — the live project config now

    async def _get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(config=drifted)

    async def _capture(conn: Any, job_id: uuid.UUID, live: Any) -> Any:
        return pinned  # the job already has a snapshot; the drifted `live` is ignored

    def _load(raw: Any) -> str:
        loaded["raw"] = raw
        return "CONFIG"

    async def _run_build(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(status="ready")

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "get_project", _get_project)
    monkeypatch.setattr(bw, "capture_config_snapshot", _capture)
    monkeypatch.setattr(bw, "load_build_config", _load)
    monkeypatch.setattr(bw, "default_stages", lambda config, **k: "STAGES")
    monkeypatch.setattr(bw, "run_build", _run_build)

    await bw.run_build_task(_ctx(), "proj", str(uuid.uuid4()))

    assert loaded["raw"] == pinned  # built from the pinned snapshot…
    assert loaded["raw"] != drifted  # …NOT the drifted live config


async def test_run_eval_task_terminalizes_on_store_error_never_strands_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: once the eval job is marked 'running', a store outage out of run_eval
    # (Neo4j/Qdrant/Postgres-read down) must terminalize the jobs row, not let the
    # error propagate. If it propagated, job_lease's finally releases the lease and
    # the row is left 'running'+unleased — which NO sweep recovers (find_reapable
    # needs a held lease; find_unenqueued needs 'queued'), permanently locking the
    # project out of every future job via create_job_exclusive. So it must end
    # 'failed', mirroring run_build's stage boundary.
    statuses: list[str] = []

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        statuses.append(fields.get("status", ""))

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("neo4j connection refused")  # a store outage, not a refusal

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr("core.eval.golden.load_golden", lambda path: "GOLDEN")
    monkeypatch.setattr("core.mcp.policy.load_query_policy", lambda path: "POLICY")
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _boom)

    ctx = {
        "engine": _FakeEngine(),
        "neo4j": _FakeNeo4j(),
        "qdrant": "QD",
        "embedder": "EMB",
        "llm": "LLM",
        "owner": "worker-abc",
    }
    result = await bw.run_eval_task(ctx, "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "failed"  # terminal, not a propagated exception
    # marked running, then FAILED — never left dangling at 'running'
    assert statuses == ["running", "failed"]


async def test_enqueue_build_uses_job_id_dedup() -> None:
    calls: dict[str, Any] = {}

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            calls["enqueue"] = (fn, args, _job_id)
            return object()  # arq accepted the enqueue

    jid = uuid.uuid4()
    accepted = await bw.enqueue_build(_Redis(), "proj", jid)  # type: ignore[arg-type]

    # arq dedups on _job_id: re-enqueuing a queued/running job is refused.
    assert calls["enqueue"] == (bw.BUILD_TASK, ("proj", str(jid)), str(jid))
    assert accepted is True


async def test_enqueue_build_reports_a_dedup_refusal() -> None:
    # WHY: the reaper's queued-sweep replays this exact call and must count only
    # NEW dispatches — arq's refusal (a dispatch already pending) returns False.
    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            return None  # a job with this id is already pending → refused

    assert await bw.enqueue_build(_Redis(), "proj", uuid.uuid4()) is False  # type: ignore[arg-type]


async def test_reenqueue_build_uses_a_deterministic_per_stale_lease_id() -> None:
    calls: list[Any] = []

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            calls.append((fn, args, _job_id))
            return object()  # arq accepted the enqueue

    jid = uuid.uuid4()
    expiry = datetime(2026, 7, 9, 1, 2, 3, tzinfo=UTC)
    first = await bw.reenqueue_build(_Redis(), "proj", jid, stale_expiry=expiry)  # type: ignore[arg-type]
    second = await bw.reenqueue_build(_Redis(), "proj", jid, stale_expiry=expiry)  # type: ignore[arg-type]

    assert first is True and second is True
    # the id is derived from (job, stale lease expiry) — NOT the job's own id (the
    # crashed dispatch's in-progress key lingers 24h and would refuse it) and NOT a
    # fresh id per call (ticks would pile up duplicates behind a saturated queue).
    # Same stale lease → byte-identical id, so arq's own dedup suppresses re-ticks.
    expected = f"reap:{jid}:{expiry.isoformat()}"
    assert [c[2] for c in calls] == [expected, expected]
    assert calls[0][:2] == (bw.BUILD_TASK, ("proj", str(jid)))


async def _no_unenqueued(
    conn: Any, grace: float
) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None]]:
    return []


async def test_reap_stuck_builds_reenqueues_each_crashed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    j1, j2 = uuid.uuid4(), uuid.uuid4()
    e1 = datetime(2026, 7, 9, 1, 0, 0, tzinfo=UTC)
    e2 = datetime(2026, 7, 9, 1, 0, 30, tzinfo=UTC)
    enq: list[Any] = []

    async def _find(conn: Any) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None, datetime]]:
        # build/ingest jobs carry no build_id on the jobs row → build-family branch
        return [(j1, "p1", "build", None, e1), (j2, "p2", "ingest", None, e2)]

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            enq.append((fn, args, _job_id))
            return object()

    monkeypatch.setattr(bw, "find_reapable_jobs", _find)
    monkeypatch.setattr(bw, "find_unenqueued_jobs", _no_unenqueued)
    reaped = await bw.reap_stuck_builds({"engine": _FakeEngine(), "redis": _Redis()})

    assert reaped == 2
    # each crashed job re-dispatched under its own per-stale-lease id
    assert enq == [
        (bw.BUILD_TASK, ("p1", str(j1)), f"reap:{j1}:{e1.isoformat()}"),
        (bw.BUILD_TASK, ("p2", str(j2)), f"reap:{j2}:{e2.isoformat()}"),
    ]


async def test_reap_stuck_builds_counts_only_new_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: while a replacement dispatch sits queued behind a saturated pool, the
    # stale row keeps matching every 30s tick. arq refuses the duplicate id
    # (enqueue_job → None) and the tick must report 0 new dispatches — the reaper
    # piles up NO duplicates for one crashed job.
    async def _find(conn: Any) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None, datetime]]:
        return [(uuid.uuid4(), "p1", "build", None, datetime(2026, 7, 9, tzinfo=UTC))]

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            return None  # arq: a job with this id is already pending → refused

    monkeypatch.setattr(bw, "find_reapable_jobs", _find)
    monkeypatch.setattr(bw, "find_unenqueued_jobs", _no_unenqueued)
    reaped = await bw.reap_stuck_builds({"engine": _FakeEngine(), "redis": _Redis()})

    assert reaped == 0  # matched one stale row, enqueued nothing new


async def test_reap_stuck_builds_is_a_noop_when_nothing_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enq: list[Any] = []

    async def _find(conn: Any) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None, datetime]]:
        return []  # no expired leases → an idle tick

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            enq.append(1)
            return object()

    monkeypatch.setattr(bw, "find_reapable_jobs", _find)
    monkeypatch.setattr(bw, "find_unenqueued_jobs", _no_unenqueued)
    reaped = await bw.reap_stuck_builds({"engine": _FakeEngine(), "redis": _Redis()})

    assert reaped == 0
    assert enq == []  # nothing enqueued on an idle tick


async def test_reap_stuck_builds_replays_lost_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (BA2e queued-sweep): a job committed `queued` whose arq enqueue was
    # lost (trigger crash window / Redis loss / a dispatch that raced the
    # trigger's commit and no-opped) has NO lease — invisible to the expired-
    # lease sweep — so the reaper must replay the trigger's own enqueue: the
    # job's OWN arq id (freed by keep_result=0 after any no-op dispatch), not
    # a reap:<...> generation id.
    j1, j2 = uuid.uuid4(), uuid.uuid4()
    enq: list[Any] = []
    seen_grace: list[float] = []

    async def _none_reapable(
        conn: Any,
    ) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None, datetime]]:
        return []

    async def _find_lost(
        conn: Any, grace: float
    ) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None]]:
        seen_grace.append(grace)
        return [(j1, "p1", "build", None), (j2, "p2", "ingest", None)]

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            enq.append((fn, args, _job_id))
            return object() if args[1] == str(j1) else None  # j2: already pending → refused

    monkeypatch.setattr(bw, "find_reapable_jobs", _none_reapable)
    monkeypatch.setattr(bw, "find_unenqueued_jobs", _find_lost)
    reaped = await bw.reap_stuck_builds({"engine": _FakeEngine(), "redis": _Redis()})

    assert reaped == 1  # j1 newly dispatched; j2's refusal (mere backlog) not counted
    assert enq == [
        (bw.BUILD_TASK, ("p1", str(j1)), str(j1)),
        (bw.BUILD_TASK, ("p2", str(j2)), str(j2)),
    ]
    # the sweep's grace comes from settings, not a hardcoded literal
    assert seen_grace == [get_settings().job_enqueue_grace_seconds]


async def test_reap_stuck_builds_counts_both_sweeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crashed, lost = uuid.uuid4(), uuid.uuid4()
    expiry = datetime(2026, 7, 10, 1, 0, 0, tzinfo=UTC)

    async def _find(conn: Any) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None, datetime]]:
        return [(crashed, "p1", "build", None, expiry)]

    async def _find_lost(
        conn: Any, grace: float
    ) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None]]:
        return [(lost, "p2", "build", None)]

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            return object()

    monkeypatch.setattr(bw, "find_reapable_jobs", _find)
    monkeypatch.setattr(bw, "find_unenqueued_jobs", _find_lost)
    reaped = await bw.reap_stuck_builds({"engine": _FakeEngine(), "redis": _Redis()})

    assert reaped == 2  # one crashed re-dispatch + one replayed lost enqueue


async def test_reap_reenqueues_crashed_eval_as_eval_task_not_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: an eval job maps to EVAL_TASK, not BUILD_TASK. Before the reaper became
    # kind-aware, a crashed eval (expired lease, non-terminal) was re-dispatched as
    # a build — running run_build against an eval job's id, corrupting recovery. It
    # must resume as an eval, carrying the job's target build_id, under the same
    # deterministic per-stale-lease reap id.
    eval_job, target_build = uuid.uuid4(), uuid.uuid4()
    expiry = datetime(2026, 7, 11, 2, 0, 0, tzinfo=UTC)
    enq: list[Any] = []

    async def _find(conn: Any) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None, datetime]]:
        return [(eval_job, "p1", "eval", target_build, expiry)]

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            enq.append((fn, args, _job_id))
            return object()

    monkeypatch.setattr(bw, "find_reapable_jobs", _find)
    monkeypatch.setattr(bw, "find_unenqueued_jobs", _no_unenqueued)
    reaped = await bw.reap_stuck_builds({"engine": _FakeEngine(), "redis": _Redis()})

    assert reaped == 1
    assert enq == [
        (
            bw.EVAL_TASK,
            ("p1", str(eval_job), str(target_build)),
            f"reap:{eval_job}:{expiry.isoformat()}",
        )
    ]


async def test_reap_replays_lost_eval_enqueue_as_eval_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (BA2e queued-sweep, eval): a lost eval dispatch (queued, no lease) must be
    # replayed as EVAL_TASK under the job's OWN arq id — the eval trigger's exact
    # enqueue_eval — not as a build.
    eval_job, target_build = uuid.uuid4(), uuid.uuid4()
    enq: list[Any] = []

    async def _none_reapable(
        conn: Any,
    ) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None, datetime]]:
        return []

    async def _find_lost(
        conn: Any, grace: float
    ) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None]]:
        return [(eval_job, "p1", "eval", target_build)]

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            enq.append((fn, args, _job_id))
            return object()

    monkeypatch.setattr(bw, "find_reapable_jobs", _none_reapable)
    monkeypatch.setattr(bw, "find_unenqueued_jobs", _find_lost)
    reaped = await bw.reap_stuck_builds({"engine": _FakeEngine(), "redis": _Redis()})

    assert reaped == 1
    assert enq == [(bw.EVAL_TASK, ("p1", str(eval_job), str(target_build)), str(eval_job))]


async def test_reap_skips_eval_job_missing_build_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: EVAL_TASK cannot run without its target build_id. That state is forbidden
    # by create_job_exclusive(kind="eval", build_id=…), but if a malformed eval row
    # ever appears the reaper must skip it (logged) rather than crash the tick —
    # which would strand every OTHER stuck job the same tick would have recovered.
    eval_job, good = uuid.uuid4(), uuid.uuid4()
    expiry = datetime(2026, 7, 11, 3, 0, 0, tzinfo=UTC)
    enq: list[Any] = []

    async def _find(conn: Any) -> list[tuple[uuid.UUID, str, str, uuid.UUID | None, datetime]]:
        # a broken eval row (no build_id) alongside a healthy build — the build
        # must still be recovered
        return [(eval_job, "p1", "eval", None, expiry), (good, "p2", "build", None, expiry)]

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> Any:
            enq.append((fn, args, _job_id))
            return object()

    monkeypatch.setattr(bw, "find_reapable_jobs", _find)
    monkeypatch.setattr(bw, "find_unenqueued_jobs", _no_unenqueued)
    reaped = await bw.reap_stuck_builds({"engine": _FakeEngine(), "redis": _Redis()})

    assert reaped == 1  # only the healthy build was re-dispatched
    assert enq == [(bw.BUILD_TASK, ("p2", str(good)), f"reap:{good}:{expiry.isoformat()}")]


async def test_on_startup_builds_the_dep_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bw, "create_async_engine", lambda *a, **k: "ENGINE")
    monkeypatch.setattr(bw, "vector_client", lambda: "QD")
    monkeypatch.setattr(bw, "graph_driver", lambda: "NEO")
    monkeypatch.setattr(bw, "embedding_model", lambda: "EMB")
    monkeypatch.setattr(bw, "chat_model", lambda: "LLM")

    ctx: dict[str, Any] = {}
    await bw.on_startup(ctx)

    assert ctx["engine"] == "ENGINE"
    assert ctx["qdrant"] == "QD"
    assert ctx["neo4j"] == "NEO"
    assert ctx["embedder"] == "EMB"
    assert ctx["llm"] == "LLM"
    assert ctx["owner"].startswith("worker-")  # a unique per-process lease owner


async def test_on_shutdown_closes_every_engine() -> None:
    closed: list[str] = []

    def _spy(name: str, method: str) -> Any:
        async def _close() -> None:
            closed.append(name)

        return SimpleNamespace(**{method: _close})

    ctx = {
        "qdrant": _spy("qdrant", "close"),
        "neo4j": _spy("neo4j", "close"),
        "engine": _spy("engine", "dispose"),
    }
    await bw.on_shutdown(ctx)

    assert set(closed) == {"qdrant", "neo4j", "engine"}


def test_worker_settings_shape() -> None:
    assert bw.WorkerSettings.functions == [bw.run_build_task, bw.run_eval_task]
    # accessed on the class, these are the plain module coroutines arq calls
    assert bw.WorkerSettings.on_startup is bw.on_startup
    assert bw.WorkerSettings.on_shutdown is bw.on_shutdown
    assert isinstance(bw.WorkerSettings.redis_settings, RedisSettings)
    # BA2d-3: the crash-recovery reaper cron is registered (runs the reaper coro,
    # deduped across workers so one worker reaps per tick)
    (reaper,) = bw.WorkerSettings.cron_jobs
    assert isinstance(reaper, CronJob)
    assert reaper.coroutine is bw.reap_stuck_builds
    assert reaper.unique is True
    # twice a minute — pins the ~1-min recovery cadence against the 60s lease TTL
    assert reaper.second == {0, 30}
    # no arq results (jobs row is the SoR): a kept result would reserve a failed
    # replacement's reap id for an hour and stall recovery to keep_result, not ~1min
    assert bw.WorkerSettings.keep_result == 0
    # crash recovery is bounded by job_timeout (arq's in-progress key), so it's a
    # modest config value, not a build-length-sized 3600
    assert bw.WorkerSettings.job_timeout == get_settings().build_job_timeout_seconds
    assert bw.WorkerSettings.max_tries == 3
