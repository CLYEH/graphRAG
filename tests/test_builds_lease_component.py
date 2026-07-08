"""Why: the integration test proves the lease's SQL semantics on live Postgres,
but the *wrapper*'s control flow — skip run_build entirely when the lease is
held, always release on exit (even when run_build raises), forward run_build's
kwargs, and stop heartbeating once the lease is lost — is pure orchestration.
These component tests spy the lease primitives + run_build (no Postgres) so that
flow is pinned in the fast lane, where the integration test can't run.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.builds import lease as lease_mod
from core.builds.lease import run_build_leased
from core.builds.orchestrator import BuildOutcome, Stages

_JOB = uuid.uuid4()
_OUTCOME = BuildOutcome(
    build_id=uuid.uuid4(), run_id=uuid.uuid4(), status="ready", cancelled=False, error=None
)


class _FakeEngine:
    """`engine.begin()` async-context-manager that yields a throwaway conn — the
    spied primitives ignore it, so it never touches a database."""

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[Any]:
        yield SimpleNamespace()


def _engine() -> Any:
    return cast(Any, _FakeEngine())


def _stages() -> Stages:
    async def _noop(conn: Any, project: str, build_id: uuid.UUID) -> Any:
        raise AssertionError("stages must not run in a component test")

    return Stages(
        ingest=_noop, clean=_noop, graph=_noop, resolve=_noop, index=_noop, summarize=_noop
    )


async def test_lease_held_skips_run_and_does_not_release(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    async def _acquire(conn: Any, job_id: uuid.UUID, owner: str, ttl: float) -> bool:
        return False  # a live peer holds it

    async def _run(*a: Any, **k: Any) -> Any:
        calls["ran"] = True

    async def _release(conn: Any, job_id: uuid.UUID, owner: str) -> None:
        calls["released"] = owner

    monkeypatch.setattr(lease_mod, "acquire_lease", _acquire)
    monkeypatch.setattr(lease_mod, "run_build", _run)
    monkeypatch.setattr(lease_mod, "release_lease", _release)

    result = await run_build_leased(_engine(), "p", _JOB, _stages(), owner="A")

    assert result is None  # deliberate no-op — the peer is executing
    assert "ran" not in calls  # run_build never called
    assert "released" not in calls  # we never held the lease, so nothing to release


async def test_acquired_runs_forwards_kwargs_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    async def _acquire(conn: Any, job_id: uuid.UUID, owner: str, ttl: float) -> bool:
        return True

    async def _run(engine: Any, project: str, job_id: uuid.UUID, stages: Any, **kw: Any) -> Any:
        calls["run_kw"] = kw
        return _OUTCOME

    async def _release(conn: Any, job_id: uuid.UUID, owner: str) -> None:
        calls["released"] = owner

    async def _renew(conn: Any, job_id: uuid.UUID, owner: str, ttl: float) -> bool:
        return True

    monkeypatch.setattr(lease_mod, "acquire_lease", _acquire)
    monkeypatch.setattr(lease_mod, "run_build", _run)
    monkeypatch.setattr(lease_mod, "release_lease", _release)
    monkeypatch.setattr(lease_mod, "renew_lease", _renew)

    # heartbeat_seconds huge so the beat never fires before the fast fake returns
    # and the finally cancels it — isolates the acquire→run→release path.
    result = await run_build_leased(
        _engine(),
        "p",
        _JOB,
        _stages(),
        owner="A",
        heartbeat_seconds=1e9,
        config_hash="ch",
        source_hash="sh",
    )

    assert result is _OUTCOME
    assert calls["released"] == "A"
    # run_build got the build kwargs verbatim (None where unset).
    assert calls["run_kw"] == {
        "build_id": None,
        "config_hash": "ch",
        "source_hash": "sh",
        "step_failure_ratio": None,
    }


async def test_release_runs_even_when_run_build_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    async def _acquire(conn: Any, job_id: uuid.UUID, owner: str, ttl: float) -> bool:
        return True

    async def _run(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    async def _release(conn: Any, job_id: uuid.UUID, owner: str) -> None:
        calls["released"] = owner

    async def _renew(conn: Any, job_id: uuid.UUID, owner: str, ttl: float) -> bool:
        return True

    monkeypatch.setattr(lease_mod, "acquire_lease", _acquire)
    monkeypatch.setattr(lease_mod, "run_build", _run)
    monkeypatch.setattr(lease_mod, "release_lease", _release)
    monkeypatch.setattr(lease_mod, "renew_lease", _renew)

    with pytest.raises(RuntimeError, match="boom"):
        await run_build_leased(_engine(), "p", _JOB, _stages(), owner="A", heartbeat_seconds=1e9)

    # the finally released the lease so a retry can re-acquire — a failed build
    # must not leave its lease stuck.
    assert calls["released"] == "A"


async def test_heartbeat_renews_until_it_loses_the_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    # renew True, True, then False → the loop renews 3× and stops on the loss
    # (an expiry-reclaim handed the lease off); no cancel needed.
    verdicts = iter([True, True, False])
    count = {"n": 0}

    async def _renew(conn: Any, job_id: uuid.UUID, owner: str, ttl: float) -> bool:
        count["n"] += 1
        return next(verdicts)

    monkeypatch.setattr(lease_mod, "renew_lease", _renew)

    await lease_mod._heartbeat(_engine(), _JOB, "A", ttl_seconds=60.0, interval=0.0)

    assert count["n"] == 3


async def test_heartbeat_survives_a_transient_renew_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # a renew that raises (a DB blip) must not propagate — it would surface from
    # `await beat` in run_build_leased's finally and mask the build result. The
    # loop skips that beat and carries on.
    count = {"n": 0}
    verdicts = iter([True, False])

    async def _renew(conn: Any, job_id: uuid.UUID, owner: str, ttl: float) -> bool:
        count["n"] += 1
        if count["n"] == 1:
            raise RuntimeError("db blip")
        return next(verdicts)

    monkeypatch.setattr(lease_mod, "renew_lease", _renew)

    # returns (does not raise) — blip(1) → renew ok(2) → lease lost(3) → stop
    await lease_mod._heartbeat(_engine(), _JOB, "A", 60.0, 0.0)

    assert count["n"] == 3


async def test_heartbeat_stops_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _renew(conn: Any, job_id: uuid.UUID, owner: str, ttl: float) -> bool:
        return True  # always ours — only a cancel ends the loop

    monkeypatch.setattr(lease_mod, "renew_lease", _renew)

    beat = asyncio.create_task(lease_mod._heartbeat(_engine(), _JOB, "A", 60.0, 0.0))
    await asyncio.sleep(0)  # let it spin a few renewals
    beat.cancel()
    with pytest.raises(asyncio.CancelledError):
        await beat
