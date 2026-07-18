"""Why: the CFG1 gateway is the owner-ratified external surface —
``http://host:port/mcp/<project>``, one port, every project, no restart on
project creation. What must hold: routing is registry-driven (unknown → 404,
known → the child app, created-after-startup → served on first request),
each project's logical server is built ONCE and cached (§9's
one-server-per-project preserved under one roof), non-addressable names
(``.``/``..``) never reach the registry, the child sees a root-relative path
with the gateway prefix in root_path, and shutdown closes every mounted
child's lifespan.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import core.mcp.gateway as gateway_module
from core.mcp.gateway import build_gateway


class _ChildApp:
    """A fake per-project ASGI app recording scopes + lifespan state."""

    def __init__(self, project: str) -> None:
        self.project = project
        self.scopes: list[dict[str, Any]] = []
        self.lifespan_entered = False
        self.lifespan_closed = False
        self.settings = SimpleNamespace(streamable_http_path="/mcp")
        self.router = SimpleNamespace(lifespan_context=self._lifespan)

    def _lifespan(self, app: object) -> Any:
        child = self

        @asynccontextmanager
        async def ctx() -> Any:
            # a REAL anyio task group, like the SDK's session manager: its
            # cancel scope is task-bound, so a gateway that exits this from a
            # DIFFERENT task than entered it raises RuntimeError — the exact
            # cross-task bug gate-2 caught; a trivial fake was false-green
            async with anyio.create_task_group():
                child.lifespan_entered = True
                yield
            child.lifespan_closed = True

        return ctx()

    def streamable_http_app(self) -> Any:
        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            self.scopes.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok:" + self.project.encode()})

        app.router = self.router  # type: ignore[attr-defined]
        return app


class _Harness:
    def __init__(self, registry: set[str]) -> None:
        self.registry = registry
        self.children: dict[str, _ChildApp] = {}
        self.build_calls: list[str] = []

    def fake_build_server(self, project: str) -> _ChildApp:
        self.build_calls.append(project)
        child = _ChildApp(project)
        self.children[project] = child
        return child

    async def fake_get_project(self, conn: object, name: str) -> object | None:
        return SimpleNamespace(name=name) if name in self.registry else None


async def _request(app: Any, path: str) -> tuple[int, bytes]:
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def receive() -> dict[str, Any]:  # pragma: no cover — no request body reads
        return {"type": "http.request", "body": b""}

    await app({"type": "http", "path": path, "method": "POST", "headers": []}, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    h = _Harness(registry={"nmmst"})
    monkeypatch.setattr(gateway_module, "build_server", h.fake_build_server)
    monkeypatch.setattr("core.registry.get_project", h.fake_get_project)
    # the gateway's engine is only handed to get_project (stubbed) — a bare
    # object satisfies the None-guard without touching Postgres
    monkeypatch.setattr(
        gateway_module,
        "create_async_engine",
        lambda *a, **k: SimpleNamespace(dispose=_async_noop, connect=_FakeConnect),
    )
    return h


async def _async_noop() -> None:
    return None


class _FakeConnect:
    def __init__(self) -> None:
        pass

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_routing_registry_and_isolation(harness: _Harness) -> None:
    app = build_gateway()

    started = anyio.Event()
    finish = anyio.Event()
    events: list[dict[str, Any]] = []

    async def lifespan_receive() -> dict[str, Any]:
        if not started.is_set():
            return {"type": "lifespan.startup"}
        await finish.wait()
        return {"type": "lifespan.shutdown"}

    async def lifespan_send(message: dict[str, Any]) -> None:
        events.append(message)
        if message["type"] == "lifespan.startup.complete":
            started.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan"}, lifespan_receive, lifespan_send)
        await started.wait()

        # unknown project → 404, registry consulted
        status, body = await _request(app, "/mcp/ghost")
        assert status == 404 and b"ghost" in body

        # non-addressable names → 404 WITHOUT a registry lookup
        assert (await _request(app, "/mcp/."))[0] == 404
        assert (await _request(app, "/mcp/.."))[0] == 404

        # outside /mcp → 404
        assert (await _request(app, "/health"))[0] == 404

        # known project → routed; child sees root-relative path + prefix root_path
        status, body = await _request(app, "/mcp/nmmst")
        assert status == 200 and body == b"ok:nmmst"
        child = harness.children["nmmst"]
        assert child.lifespan_entered
        assert child.scopes[0]["path"] == "/"
        assert child.scopes[0]["root_path"] == "/mcp/nmmst"
        # the child's own streamable path was re-rooted so the gateway prefix
        # owns the URL (no /mcp/nmmst/mcp double-segment)
        assert child.settings.streamable_http_path == "/"

        # second request reuses the cached instance — §9 one-server-per-project
        await _request(app, "/mcp/nmmst/sub")
        assert harness.build_calls == ["nmmst"]
        assert child.scopes[1]["path"] == "/sub"

        # a project created AFTER startup serves on its first request
        harness.registry.add("fresh")
        status, body = await _request(app, "/mcp/fresh")
        assert status == 200 and body == b"ok:fresh"
        assert harness.build_calls == ["nmmst", "fresh"]

        finish.set()

    # gateway shutdown closed every mounted child's lifespan
    assert harness.children["nmmst"].lifespan_closed
    assert harness.children["fresh"].lifespan_closed
    assert events[-1]["type"] == "lifespan.shutdown.complete"
