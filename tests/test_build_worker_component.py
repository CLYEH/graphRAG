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


async def _lock_active(conn: Any, job_id: uuid.UUID) -> Any:
    # lock_job stand-in for the eval task's pre-start terminal-job guard: a LIVE
    # (running) job, so the guard proceeds instead of no-opping. The eval task also
    # calls lock_job at finalize (via _eval_leads), so this is safe for both.
    return SimpleNamespace(status="running")


async def _holds_lease_yes(conn: Any, job_id: uuid.UUID, owner: str) -> bool:
    # holds_lease stand-in: this worker still OWNS the lease, so the eval finalize /
    # failure guard (_eval_leads) proceeds to write instead of no-opping to a peer.
    return True


async def _no_fingerprint_pin(conn: Any, job_id: uuid.UUID) -> None:
    # get_eval_inputs_fingerprint stand-in: NO accept-time pin (a pre-pin job), so the
    # eval task's input-drift guard is skipped — isolates a test from that check.
    return None


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

    async def _not_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False  # no cancel — this test isolates the store-outage path

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_active)
    monkeypatch.setattr(bw, "holds_lease", _holds_lease_yes)
    monkeypatch.setattr(bw, "is_cancel_requested", _not_cancelled)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
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


async def test_run_eval_task_no_ops_when_lease_lost_before_marking_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (triage 32): a large SYNCHRONOUS preflight load (golden/policy YAML) can block
    # the event loop past the lease TTL — the heartbeat can't renew, the lease lapses, and
    # the reaper hands this job to a REPLACEMENT that terminalizes + releases it. An
    # UNCONDITIONAL 'running' write would REOPEN that terminal job as 'running' while this
    # worker no longer leads; finalize's _eval_leads then no-ops, stranding it
    # 'running'+unleased (no sweep recovers it → create_job_exclusive blocks the project).
    # So the 'running' mark is gated on still LEADING (lease-owning + live) under the row
    # lock: if a replacement took over, this worker no-ops (None) and never reopens the job.
    # Here holds_lease=False models the lapsed-lease handoff. Revert-probe: drop the guard
    # and 'running' is written (and run_eval fires) even though we lost the lease.
    statuses: list[str] = []
    ran = {"n": 0}

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        statuses.append(fields.get("status", ""))

    async def _lease_lost(conn: Any, job_id: uuid.UUID, owner: str) -> bool:
        return False  # a replacement reclaimed the lapsed lease during the preflight stall

    async def _not_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False

    async def _run_eval(*a: Any, **k: Any) -> str:
        ran["n"] += 1  # must NOT run — we no longer lead
        return "REPORT"

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_active)  # pre-start guard sees a live job
    monkeypatch.setattr(bw, "holds_lease", _lease_lost)  # but the lease was lost by mark-running
    monkeypatch.setattr(bw, "is_cancel_requested", _not_cancelled)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", lambda path: "GOLDEN")
    monkeypatch.setattr("core.mcp.policy.load_query_policy", lambda path: "POLICY")
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _run_eval)

    result = await bw.run_eval_task(_ctx(), "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result is None  # benign no-op — the replacement is authoritative
    assert "running" not in statuses  # never REOPENED the terminalized job as 'running'
    assert ran["n"] == 0  # run_eval never ran


async def test_run_eval_task_terminalizes_on_a_preflight_loader_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: preflight (load the golden set / policy, pick models) is all-LOCAL, so a
    # bad golden PATH is a deterministic refusal but NOT one of the expected
    # GoldenError/PolicyError types — a directory / bad perms raises OSError, invalid
    # UTF-8 raises UnicodeDecodeError. If such an error escaped, job_lease releases
    # the lease but the row stays 'queued'+unleased: the queued-sweep replays the bad
    # eval FOREVER and create_job_exclusive blocks every later job for the project. So
    # it must terminalize 'failed', BEFORE the 'running' mark (never a 'running' here).
    statuses: list[str] = []

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        statuses.append(fields.get("status", ""))

    async def _not_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False

    def _boom_load(path: Any) -> Any:
        raise OSError("golden.yaml is a directory")  # a preflight I/O error, not GoldenError

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_active)
    monkeypatch.setattr(
        bw, "holds_lease", _holds_lease_yes
    )  # still leads → _fail_eval marks failed
    monkeypatch.setattr(bw, "is_cancel_requested", _not_cancelled)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", _boom_load)

    ctx = {"engine": _FakeEngine(), "neo4j": _FakeNeo4j(), "owner": "worker-abc"}
    result = await bw.run_eval_task(ctx, "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "failed"  # terminal, never left 'queued' for the sweep to loop
    assert statuses == ["failed"]  # failed at preflight, before any 'running' mark


async def test_run_eval_task_preflight_failure_no_ops_after_lease_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (triage 34): a LARGE synchronous golden/policy load can block the event loop past
    # the lease TTL BEFORE it raises — the heartbeat can't renew, the lease lapses, and the
    # reaper hands this job to a replacement that may already be running/finished. An
    # UNCONDITIONAL _fail_job on the preflight error would clobber the replacement's
    # result/status with 'failed'. So the preflight failure terminalizes via _fail_eval,
    # which re-checks lead under the row lock: a stale worker (holds_lease False) no-ops
    # (None) and writes NOTHING. Revert-probe: use the unconditional _fail_job and 'failed'
    # is written despite the handoff.
    statuses: list[str] = []

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        statuses.append(fields.get("status", ""))

    async def _lease_lost(conn: Any, job_id: uuid.UUID, owner: str) -> bool:
        return False  # the lease was reclaimed by a replacement during the slow preflight

    async def _not_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False

    def _boom_load(path: Any) -> Any:
        raise OSError("golden.yaml is a directory")  # the preflight failure after the stall

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_active)
    monkeypatch.setattr(bw, "holds_lease", _lease_lost)  # we no longer lead by the failure
    monkeypatch.setattr(bw, "is_cancel_requested", _not_cancelled)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", _boom_load)

    ctx = {"engine": _FakeEngine(), "neo4j": _FakeNeo4j(), "owner": "worker-abc"}
    result = await bw.run_eval_task(ctx, "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result is None  # benign no-op — the replacement is authoritative
    assert statuses == []  # never wrote 'failed' over the replacement's status


async def test_run_eval_task_fails_loud_when_eval_inputs_drifted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (triage 27): the endpoint pins the ACCEPT-time golden+policy fingerprint on the
    # job. If a user edits those between the 202 and dispatch, the worker must NOT score
    # the new bytes (the idempotency key is scoped to the OLD fingerprint) — it re-
    # fingerprints the live inputs in preflight and, on a mismatch, terminalizes the job
    # 'failed' BEFORE running (no run_eval, no dangling 'running'). Revert-probe: drop the
    # drift guard and preflight passes, so run_eval fires on the drifted inputs (ran['n']
    # → 1) — the exact harm, scoring bytes the client never accepted.
    statuses: list[str] = []
    ran = {"n": 0}

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        statuses.append(fields.get("status", ""))

    async def _pinned(conn: Any, job_id: uuid.UUID) -> str:
        return "accepted-fingerprint"  # what the endpoint pinned at accept time

    async def _not_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False

    async def _run_eval(*a: Any, **k: Any) -> str:
        ran["n"] += 1  # must NOT run — the inputs drifted from what was accepted
        return "REPORT"

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_active)
    monkeypatch.setattr(
        bw, "holds_lease", _holds_lease_yes
    )  # still leads → _fail_eval marks failed
    monkeypatch.setattr(bw, "is_cancel_requested", _not_cancelled)
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _pinned)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    # a SINGLE read of the live inputs whose joint fingerprint DIFFERS from what was
    # accepted (an edit since the 202). The one-read seam (triage 35) is also what closes
    # the TOCTOU: the same bytes it fingerprints are the bytes the parse would score.
    monkeypatch.setattr(
        "core.eval.idempotency.read_and_fingerprint_eval_inputs",
        lambda root: ("live-DIFFERENT", b"golden", b"policy"),
    )
    # mocked so that WITHOUT the drift guard preflight passes and run_eval fires on the
    # drifted inputs (the revert path this probe guards against); **kw tolerates the
    # worker passing the already-read text= on the pinned path.
    monkeypatch.setattr("core.eval.golden.load_golden", lambda path, **kw: "GOLDEN")
    monkeypatch.setattr("core.mcp.policy.load_query_policy", lambda path, **kw: "POLICY")
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _run_eval)

    result = await bw.run_eval_task(_ctx(), "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "failed"  # terminalized, never scored the drifted inputs
    assert ran["n"] == 0  # run_eval never called
    assert statuses == ["failed"]  # failed at preflight, BEFORE the 'running' mark


async def test_run_eval_task_pinned_eval_parses_the_fingerprinted_bytes_not_a_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (triage 35): the drift guard is only sound if the bytes it FINGERPRINTS are the bytes
    # it PARSES. A pinned eval reads golden+policy ONCE (read_and_fingerprint_eval_inputs) and
    # must hand THOSE bytes to the loaders — if load_golden/load_query_policy re-opened the paths
    # instead, an edit landing between the fingerprint and the parse would be scored under the
    # matched-but-now-stale fingerprint, reopening the very TOCTOU the guard closes. Proof: on a
    # MATCHING fingerprint (no drift) the loaders receive text= equal to the single read's bytes.
    # Revert-probe: drop the text= plumbing and the loaders get text=None (a path re-read), which
    # this asserts against.
    seen: dict[str, str | None] = {}

    async def _noop_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        pass

    async def _pinned_matches(conn: Any, job_id: uuid.UUID) -> str:
        return "MATCH"  # equals the live fingerprint below → guard passes, parse proceeds

    async def _never_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False

    async def _run_eval(*a: Any, **k: Any) -> str:
        return "REPORT"

    async def _persist(conn: Any, report: Any) -> None:
        pass

    def _capture_golden(path: Any, *, text: str | None = None) -> str:
        seen["golden"] = text  # the loader must be handed the fingerprinted bytes, not re-read
        return "GOLDEN"

    def _capture_policy(path: Any, *, text: str | None = None) -> str:
        seen["policy"] = text
        return "POLICY"

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _noop_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_active)
    monkeypatch.setattr(bw, "holds_lease", _holds_lease_yes)
    monkeypatch.setattr(bw, "is_cancel_requested", _never_cancelled)
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _pinned_matches)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    # the ONE read: fingerprint MATCHES the pin (no drift), and carries the exact bytes the
    # loaders must parse — the same in-memory bytes that were fingerprinted, never a path re-read.
    monkeypatch.setattr(
        "core.eval.idempotency.read_and_fingerprint_eval_inputs",
        lambda root: ("MATCH", b"golden-YAML", b"policy-YAML"),
    )
    monkeypatch.setattr("core.eval.golden.load_golden", _capture_golden)
    monkeypatch.setattr("core.mcp.policy.load_query_policy", _capture_policy)
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _run_eval)
    monkeypatch.setattr("core.eval.runner.persist_build_eval", _persist)

    result = await bw.run_eval_task(_ctx(), "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "done"
    # the loaders parsed the SINGLE read's bytes (decoded), never re-opened the path (text=None)
    assert seen == {"golden": "golden-YAML", "policy": "policy-YAML"}


async def test_run_eval_task_rejects_project_name_escaping_projects_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: the eval worker reads <projects_dir>/<project>/{eval/golden.yaml,
    # config.yaml}. A project named '..' would read config OUTSIDE the projects
    # root (a traversal, the same class the upload corpus guards). It must fail the
    # job BEFORE any on-disk config read — never load a file outside the root.
    statuses: list[str] = []
    loaded: list[str] = []

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        statuses.append(fields.get("status", ""))

    def _load_golden(path: Any) -> Any:
        loaded.append(str(path))  # must NOT run for an unsafe project name
        return "GOLDEN"

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", _load_golden)

    ctx = {"engine": _FakeEngine(), "neo4j": _FakeNeo4j(), "owner": "worker-abc"}
    result = await bw.run_eval_task(ctx, "..", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "failed"
    assert loaded == []  # the traversal read never happened
    assert statuses == ["failed"]  # failed at the guard, before the 'running' mark


async def test_run_eval_task_honors_cancellation_before_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY: /jobs/{id}/cancel flags cancel_requested cooperatively. An eval accepted
    # for cancellation must NOT run to completion and report success — it is
    # terminalized 'cancelled' before any work (no golden load, no run_eval).
    statuses: list[str] = []
    loaded: list[str] = []

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        statuses.append(fields.get("status", ""))

    async def _cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return True

    def _load_golden(path: Any) -> Any:
        loaded.append(str(path))  # must NOT run for a cancelled job
        return "GOLDEN"

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_active)
    monkeypatch.setattr(bw, "is_cancel_requested", _cancelled)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", _load_golden)

    ctx = {"engine": _FakeEngine(), "neo4j": _FakeNeo4j(), "owner": "worker-abc"}
    result = await bw.run_eval_task(ctx, "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "cancelled"
    assert loaded == []  # no eval work started
    assert statuses == ["cancelled"]  # terminalized before the 'running' mark


async def test_run_eval_task_noops_when_job_already_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (triage 19): the execution lease is status-BLIND, so a stale-lease reaper
    # race can hand a worker a row that is ALREADY terminal — the reaper enqueued a
    # replacement while the original was only STARVED, then the original finished
    # (status → done) and released its lease, which this replacement then acquired.
    # Re-running would reopen the finished eval and OVERWRITE builds.eval (the §20
    # gate's input). The pre-start guard LOCKs the row and, finding it no longer
    # queued/running, does NO work: no 'running' mark, no preflight, no run_eval, no
    # persist — a benign no-op (None), mirroring run_build's BuildNotResumableError.
    # Revert-probe: drop the guard and the job is re-marked running and re-persisted.
    events: list[str] = []

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        events.append(f"status={fields.get('status')}")

    async def _lock_terminal(conn: Any, job_id: uuid.UUID) -> Any:
        return SimpleNamespace(status="done")  # already finished by the original worker

    async def _never_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False  # isolate the terminal-status guard from the cancel path

    def _load_golden(path: Any) -> Any:
        events.append("load_golden")  # must NOT run for a terminal job
        return "GOLDEN"

    async def _run_eval(*a: Any, **k: Any) -> str:
        events.append("run_eval")  # must NOT run
        return "REPORT"

    async def _persist(conn: Any, report: Any) -> None:
        events.append("persist")  # must NOT overwrite builds.eval

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_terminal)
    monkeypatch.setattr(bw, "is_cancel_requested", _never_cancelled)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", _load_golden)
    monkeypatch.setattr("core.mcp.policy.load_query_policy", lambda path: "POLICY")
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _run_eval)
    monkeypatch.setattr("core.eval.runner.persist_build_eval", _persist)

    result = await bw.run_eval_task(_ctx(), "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result is None  # benign no-op, not a re-run
    assert events == []  # nothing happened: no running mark, no preflight, no run, no persist


async def test_run_eval_task_cancel_during_run_finalizes_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a cancel that arrives AFTER the pre-start check but during run_eval finalizes
    # the job 'cancelled', not 'done' (mirrors run_build's terminalize). The finalize
    # must LOCK the row (FOR UPDATE) BEFORE reading cancel_requested — the lock is the
    # cutoff (class-10: the decisive read lives under the write's lock, not a prior
    # unlocked SELECT), so we also pin lock→read ordering.
    checks = {"n": 0}
    statuses: list[str] = []
    events: list[str] = []

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        statuses.append(fields.get("status", ""))

    async def _cancel_on_finalize(conn: Any, job_id: uuid.UUID) -> bool:
        checks["n"] += 1
        events.append(f"cancel_read#{checks['n']}")
        return checks["n"] > 1  # False at the pre-start check, True at finalize

    async def _lock_job(conn: Any, job_id: uuid.UUID) -> Any:
        events.append("lock")  # the FOR UPDATE cutoff — pre-start guard AND finalize read
        return SimpleNamespace(status="running")  # live job → both guards proceed

    async def _run_eval(*a: Any, **k: Any) -> None:
        return None

    async def _persist(conn: Any, report: Any) -> None:
        events.append("persist")  # must NOT run on the cancelled path

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "is_cancel_requested", _cancel_on_finalize)
    monkeypatch.setattr(bw, "lock_job", _lock_job)
    monkeypatch.setattr(bw, "holds_lease", _holds_lease_yes)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", lambda path: "GOLDEN")
    monkeypatch.setattr("core.mcp.policy.load_query_policy", lambda path: "POLICY")
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _run_eval)
    monkeypatch.setattr("core.eval.runner.persist_build_eval", _persist)

    ctx = {
        "engine": _FakeEngine(),
        "neo4j": _FakeNeo4j(),
        "qdrant": "QD",
        "embedder": "EMB",
        "llm": "LLM",
        "owner": "worker-abc",
    }
    result = await bw.run_eval_task(ctx, "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "cancelled"
    assert statuses == ["running", "cancelled"]  # ran, then cancelled at finalize
    # Three FOR-UPDATE locks, each BEFORE the decision it guards: the pre-start guard
    # (lock → cancel_read#1, False = still running), the mark-running lead re-check
    # (triage 32: lock, no cancel read — it only confirms we still lead before writing
    # 'running'), and the finalize (lock → cancel_read#2, True). A cancel is always
    # decided under the lock, never lost to an unlocked read. And a cancelled eval NEVER
    # persists builds.eval (no "persist") — the report is withheld, nothing for the §20
    # gate to read.
    assert events == ["lock", "cancel_read#1", "lock", "lock", "cancel_read#2"]


async def test_run_eval_task_cancelled_eval_never_persists_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (triage 17 / Finding F): the §20 activation gate reads builds.eval WITHOUT
    # consulting the eval job, so builds.eval must be committed ONLY when the eval is not
    # cancelled. run_eval(persist=False) computes but does NOT write; the worker persists
    # in the finalize txn only if not cancelled — so a cancelled eval leaves NO report the
    # gate could read (no transient write, no revert window). Revert-probe: persist
    # unconditionally and `persisted` is non-empty.
    persisted: list[Any] = []
    checks = {"n": 0}

    async def _noop_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        pass

    async def _noop_lock(conn: Any, job_id: uuid.UUID) -> Any:
        return SimpleNamespace(status="running")  # live job → the pre-start guard proceeds

    async def _cancel_on_finalize(conn: Any, job_id: uuid.UUID) -> bool:
        checks["n"] += 1
        return checks["n"] > 1  # False at pre-start, True at finalize (cancel mid-run)

    async def _persist(conn: Any, report: Any) -> None:
        persisted.append(report)

    async def _run_eval(*a: Any, **k: Any) -> str:
        return "REPORT"  # computed but unpersisted (persist=False)

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _noop_progress)
    monkeypatch.setattr(bw, "is_cancel_requested", _cancel_on_finalize)
    monkeypatch.setattr(bw, "lock_job", _noop_lock)
    monkeypatch.setattr(bw, "holds_lease", _holds_lease_yes)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", lambda path: "GOLDEN")
    monkeypatch.setattr("core.mcp.policy.load_query_policy", lambda path: "POLICY")
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _run_eval)
    monkeypatch.setattr("core.eval.runner.persist_build_eval", _persist)

    result = await bw.run_eval_task(_ctx(), "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "cancelled"
    assert persisted == []  # a cancelled eval commits NOTHING to builds.eval


async def test_run_eval_task_completed_eval_persists_the_report_in_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the other side of Finding F: a COMPLETED (not cancelled) eval commits the report
    # run_eval computed — exactly once, in the finalize txn (the one canonical write).
    persisted: list[Any] = []

    async def _noop_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        pass

    async def _noop_lock(conn: Any, job_id: uuid.UUID) -> Any:
        return SimpleNamespace(status="running")  # live job → the pre-start guard proceeds

    async def _never_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False

    async def _persist(conn: Any, report: Any) -> None:
        persisted.append(report)

    async def _run_eval(*a: Any, **k: Any) -> str:
        return "REPORT"

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _noop_progress)
    monkeypatch.setattr(bw, "is_cancel_requested", _never_cancelled)
    monkeypatch.setattr(bw, "lock_job", _noop_lock)
    monkeypatch.setattr(bw, "holds_lease", _holds_lease_yes)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", lambda path: "GOLDEN")
    monkeypatch.setattr("core.mcp.policy.load_query_policy", lambda path: "POLICY")
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _run_eval)
    monkeypatch.setattr("core.eval.runner.persist_build_eval", _persist)

    result = await bw.run_eval_task(_ctx(), "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result == "done"
    assert persisted == ["REPORT"]  # the computed report is written exactly once


async def test_run_eval_task_stale_worker_does_not_overwrite_after_lease_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WHY (triage 21): the lease is a LIVENESS layer — if this worker's heartbeat lapses,
    # the reaper hands execution to a replacement (its acquire_lease reassigns
    # lease_owner). This now-stale worker can still reach the finalize; ignoring
    # lock_job's result it would persist its report and mark the job terminal,
    # OVERWRITING the replacement's builds.eval/status (or hiding its failure). The
    # finalize re-checks UNDER the row lock that we still OWN the lease (_eval_leads); a
    # reclaimed worker (holds_lease False) does a benign no-op (None) — no persist, no
    # terminal write. Revert-probe: drop the ownership check and the stale report
    # overwrites builds.eval and marks the job done. (We still LEAD when we mark running —
    # the handoff happens DURING the long run — so holds_lease is True at the mark-running
    # lead check (triage 32) and False by the finalize.)
    statuses: list[str] = []
    persisted: list[Any] = []
    lease_calls = {"n": 0}

    async def _set_progress(conn: Any, job_id: uuid.UUID, **fields: Any) -> None:
        if "status" in fields:
            statuses.append(fields["status"])

    async def _lock_running(conn: Any, job_id: uuid.UUID) -> Any:
        return SimpleNamespace(status="running")  # the job the replacement is running

    async def _lease_reclaimed(conn: Any, job_id: uuid.UUID, owner: str) -> bool:
        # led at mark-running, then the reaper handed the lease to a replacement during the
        # run — so the finalize's lead check finds it gone.
        lease_calls["n"] += 1
        return lease_calls["n"] == 1  # True at mark-running, False at finalize

    async def _never_cancelled(conn: Any, job_id: uuid.UUID) -> bool:
        return False

    async def _persist(conn: Any, report: Any) -> None:
        persisted.append(report)  # must NOT overwrite the replacement's builds.eval

    async def _run_eval(*a: Any, **k: Any) -> str:
        return "REPORT"

    monkeypatch.setattr(bw, "job_lease", _fake_lease())
    monkeypatch.setattr(bw, "set_progress", _set_progress)
    monkeypatch.setattr(bw, "lock_job", _lock_running)
    monkeypatch.setattr(bw, "holds_lease", _lease_reclaimed)
    monkeypatch.setattr(bw, "is_cancel_requested", _never_cancelled)
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(projects_dir="proj_root"))
    monkeypatch.setattr(bw, "get_eval_inputs_fingerprint", _no_fingerprint_pin)
    monkeypatch.setattr("core.eval.golden.load_golden", lambda path: "GOLDEN")
    monkeypatch.setattr("core.mcp.policy.load_query_policy", lambda path: "POLICY")
    monkeypatch.setattr("core.eval.runner.models_needed", lambda g, p: (False, False))
    monkeypatch.setattr("core.eval.runner.run_eval", _run_eval)
    monkeypatch.setattr("core.eval.runner.persist_build_eval", _persist)

    result = await bw.run_eval_task(_ctx(), "proj", str(uuid.uuid4()), str(uuid.uuid4()))

    assert result is None  # benign no-op — the replacement is authoritative
    assert persisted == []  # the stale report NEVER overwrites builds.eval
    assert statuses == ["running"]  # ran, but wrote NO terminal status (done/failed/cancelled)


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
