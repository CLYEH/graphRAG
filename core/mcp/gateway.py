"""The multi-project MCP gateway (CFG1) — one process, every project.

Owner-ratified shape (2026-07-17): ``http://<host>:<port>/mcp/<project>`` —
one port, path-per-project, and a freshly created project is servable
WITHOUT a restart. §9's「一專案一 MCP server」survives intact: each project
still gets its own logical :func:`~core.mcp.server.build_server` instance
(own lifespan, own session manager, own policy read); the gateway only
routes by path and manages their lifecycles under one ASGI app.

Mechanics:

- **Lazy mount from the registry**: the first request for ``/mcp/<name>``
  looks the project up in ``projects`` (the SoR — the same table the
  Console writes); unknown → 404 JSON, known → that project's FastMCP
  streamable-http app is built, its lifespan entered, and the instance
  cached for every later request. No restart on project creation — the
  NEXT request simply finds the new row. (A DELETED project's mounted app
  keeps serving until gateway restart — its sessions fail loud at the
  next lifespan/policy read anyway; eviction is future work, noted.)
- **Path addressability** mirrors the Console rule
  (``web/src/project/projectRoute.ts isPathAddressable``): a name that is
  ``.``/``..`` cannot ride a URL path segment — 404, same answer as
  unknown (a ``/`` never reaches us as part of one segment).
- **Auth**: none (§23 placeholder — owner 2026-07-17 default: the gateway
  ships without auth; operate on localhost/LAN/tunnel until the auth
  DR-002 round).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

import anyio
from anyio.abc import TaskGroup, TaskStatus
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.mcp.server import build_server

#: matched against the RAW (undecoded) path: a percent-encoded slash
#: (%2F) must stay INSIDE its segment — matching the decoded path would let
#: /mcp/a%2Fb smuggle itself into project `a` + child path /b and serve the
#: WRONG project's server (Codex #93 R3)
_MCP_PATH_RAW = re.compile(rb"^/mcp/([^/]+)(/.*)?$")


def _json_response(status: int, payload: dict[str, Any]) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


class McpGateway:
    """ASGI app: routes ``/mcp/<project>`` to lazily-mounted project servers."""

    def __init__(self) -> None:
        self._apps: dict[str, Any] = {}
        self._lock = anyio.Lock()
        self._engine: AsyncEngine | None = None
        # child lifespans are HOSTED: each project's lifespan is entered and
        # exited inside its own long-lived host task spawned into this
        # lifespan-owned task group. anyio cancel scopes are task-bound — the
        # SDK's StreamableHTTPSessionManager.run() enters a task group, and
        # entering it in a request task while closing from the shutdown task
        # raises `RuntimeError: Attempted to exit cancel scope in a different
        # task` (gate-2 reproduced it); one task per child owns both ends.
        self._tasks: TaskGroup | None = None
        self._stop = anyio.Event()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"unsupported ASGI scope type {scope['type']!r}")
        raw_path = scope.get("raw_path") or scope["path"].encode("utf-8")
        match = _MCP_PATH_RAW.match(raw_path)
        if match is None:
            await self._send_json(
                send, 404, {"error": "unknown path — projects are served at /mcp/<project>"}
            )
            return
        # decode the SEGMENT (not the whole path): an encoded slash decodes
        # into the name here and is rejected below, never re-split as a path
        project = unquote(match.group(1).decode("utf-8", "replace"))
        rest = unquote((match.group(2) or b"/").decode("utf-8", "replace"))
        if "/" in project:
            await self._send_json(
                send,
                404,
                {"error": f"project {project!r} is not in the registry (or not path-addressable)"},
            )
            return
        app = await self._app_for(project)
        if app is None:
            await self._send_json(
                send,
                404,
                {"error": f"project {project!r} is not in the registry (or not path-addressable)"},
            )
            return
        # the mounted app sees itself at root — root_path keeps URL
        # reconstruction (and the SDK's own endpoint echoes) correct
        child_scope = {
            **scope,
            "path": rest,
            "root_path": scope.get("root_path", "") + f"/mcp/{project}",
        }
        await app(child_scope, receive, send)

    async def _lifespan(self, receive: Any, send: Any) -> None:
        message = await receive()
        assert message["type"] == "lifespan.startup"
        try:
            settings = get_settings()
            self._engine = create_async_engine(
                settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
                poolclass=NullPool,
            )
        except Exception as exc:  # noqa: BLE001 — startup failure must be reported, not raised past the protocol
            await send({"type": "lifespan.startup.failed", "message": str(exc)})
            return
        try:
            async with anyio.create_task_group() as tasks:
                # the task group must EXIST before uvicorn starts dispatching
                # requests — startup.complete is the green light, and a first
                # /mcp/<project> in the gap would 500 on the not-yet-assigned
                # group (Codex #93 R1)
                self._tasks = tasks
                await send({"type": "lifespan.startup.complete"})
                message = await receive()
                assert message["type"] == "lifespan.shutdown"
                # release every host task — each exits its child lifespan in
                # the SAME task that entered it; the task-group exit below
                # waits for all of them to finish closing
                self._stop.set()
        finally:
            self._tasks = None
            if self._engine is not None:
                await self._engine.dispose()
            await send({"type": "lifespan.shutdown.complete"})

    async def _app_for(self, project: str) -> Any | None:
        """The project's mounted ASGI app, lazily built — None when the name
        is not addressable or not in the registry."""
        if project in (".", ".."):
            return None  # the Console's isPathAddressable rule, mirrored
        async with self._lock:
            if project in self._apps:
                return self._apps[project]
            if not await self._project_exists(project):
                return None
            server = build_server(project)
            # the child serves at ITS root — the gateway prefix owns the path
            server.settings.streamable_http_path = "/"
            app = server.streamable_http_app()
            assert self._tasks is not None, "gateway lifespan not started"

            async def host(*, task_status: TaskStatus[None]) -> None:
                # ONE task owns the child lifespan end to end (see __init__):
                # started() only fires after a successful enter, so a failing
                # child startup propagates to the mount request loud
                async with app.router.lifespan_context(app):
                    task_status.started()
                    await self._stop.wait()

            await self._tasks.start(host)
            self._apps[project] = app
            return app

    async def _project_exists(self, project: str) -> bool:
        from core.registry import get_project

        assert self._engine is not None, "gateway lifespan not started"
        async with self._engine.connect() as conn:
            return await get_project(conn, project) is not None

    @staticmethod
    async def _send_json(send: Any, status: int, payload: dict[str, Any]) -> None:
        status, body = _json_response(status, payload)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_gateway() -> McpGateway:
    """The gateway ASGI app ``graphrag serve-mcp`` runs (CFG1)."""
    return McpGateway()
