"""Why: the worker is pure wiring — read a job's project config, build the six
stages off the long-lived dep bundle, and hand them to run_build_leased under a
per-worker owner id; plus the startup/shutdown lifecycle and the enqueue helper.
These component tests spy every dependency (no Redis/Postgres/Qdrant/Neo4j/LLM)
so the config→stages→lease arg flow, the lifecycle, and the _job_id dedup are
pinned in the fast lane, where the real-worker integration test can't run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from arq.connections import RedisSettings

from api.workers import build_worker as bw


class _FakeEngine:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
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

    async def _leased(
        engine: Any, project: str, job_id: uuid.UUID, stages: Any, *, owner: str
    ) -> Any:
        calls["leased"] = (project, job_id, stages, owner)
        return SimpleNamespace(status="ready")

    monkeypatch.setattr(bw, "get_project", _get_project)
    monkeypatch.setattr(bw, "load_build_config", _load_config)
    monkeypatch.setattr(bw, "default_stages", _default_stages)
    monkeypatch.setattr(bw, "run_build_leased", _leased)

    jid = uuid.uuid4()
    result = await bw.run_build_task(_ctx(), "proj", str(jid))

    assert result == "ready"
    assert calls["project"] == "proj"
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
    assert calls["leased"] == ("proj", jid, "STAGES", "worker-abc")


async def test_run_build_task_returns_none_when_lease_held(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(config={})

    async def _leased(*a: Any, **k: Any) -> Any:
        return None  # a live peer holds the lease

    monkeypatch.setattr(bw, "get_project", _get_project)
    monkeypatch.setattr(bw, "load_build_config", lambda raw: "CONFIG")
    monkeypatch.setattr(bw, "default_stages", lambda config, **k: "STAGES")
    monkeypatch.setattr(bw, "run_build_leased", _leased)

    result = await bw.run_build_task(_ctx(), "proj", str(uuid.uuid4()))

    assert result is None  # the no-op dispatch surfaces as a None result


async def test_run_build_task_missing_project_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get_project(conn: Any, name: str) -> Any:
        return None

    monkeypatch.setattr(bw, "get_project", _get_project)

    with pytest.raises(LookupError, match="does not exist"):
        await bw.run_build_task({"engine": _FakeEngine()}, "gone", str(uuid.uuid4()))


async def test_enqueue_build_uses_job_id_dedup() -> None:
    calls: dict[str, Any] = {}

    class _Redis:
        async def enqueue_job(self, fn: str, *args: Any, _job_id: str | None = None) -> None:
            calls["enqueue"] = (fn, args, _job_id)

    jid = uuid.uuid4()
    await bw.enqueue_build(_Redis(), "proj", jid)  # type: ignore[arg-type]

    # arq dedups on _job_id: re-enqueuing a queued/running job is refused.
    assert calls["enqueue"] == (bw.BUILD_TASK, ("proj", str(jid)), str(jid))


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
    assert bw.WorkerSettings.functions == [bw.run_build_task]
    # accessed on the class, these are the plain module coroutines arq calls
    assert bw.WorkerSettings.on_startup is bw.on_startup
    assert bw.WorkerSettings.on_shutdown is bw.on_shutdown
    assert isinstance(bw.WorkerSettings.redis_settings, RedisSettings)
    assert bw.WorkerSettings.job_timeout == 3600
    assert bw.WorkerSettings.max_tries == 3
