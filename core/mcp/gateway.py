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
from core.mcp.policy import PolicyError, load_runtime_config_from_registry
from core.mcp.server import build_server
from core.metadata.schema import MetadataConfigError
from core.stores.errors import STORE_CLIENT_ERRORS

#: matched against the RAW (undecoded) path: a percent-encoded slash
#: (%2F) must stay INSIDE its segment — matching the decoded path would let
#: /mcp/a%2Fb smuggle itself into project `a` + child path /b and serve the
#: WRONG project's server (Codex #93 R3)
_MCP_PATH_RAW = re.compile(rb"^/mcp/([^/]+)(/.*)?$")

#: MCP12: ceiling on the per-request registry preflight. Measured defect: a
#: session lifespan failure (bad policy, Postgres down) surfaced as HTTP 200
#: + session id + a ZERO-BYTE stream — the PolicyError's actionable text
#: never reached the wire, and with Postgres down the hang ran 46s (past
#: every §21 budget). The preflight bounds that path: policy problems answer
#: 503 WITH the actionable message, a dead registry answers 503 fast.
_PREFLIGHT_TIMEOUT_S = 5.0


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
        # one stop event PER child (Codex #132 r1): eviction of a deleted
        # project must close THAT child's lifespan promptly — a single
        # gateway-wide event kept evicted hosts (and their session managers)
        # alive until full shutdown, accumulating orphans across
        # delete/recreate cycles. Shutdown sets every remaining event.
        self._child_stops: dict[str, anyio.Event] = {}

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
        if "/" in project or project in (".", ".."):
            # non-addressable names answer 404 BEFORE any registry read —
            # the same rule _app_for enforces (kept there as defense in
            # depth); the preflight below must not spend a DB read on them
            await self._send_json(
                send,
                404,
                {"error": f"project {project!r} is not in the registry (or not path-addressable)"},
            )
            return
        # MCP12 preflight — EVERY request, cached mount or not: the child
        # server reads its policy per SESSION lifespan, and a failure there
        # surfaces as HTTP 200 + an empty stream (the SDK gives the error no
        # wire shape), which an agent cannot distinguish from a gateway
        # crash or a network flap. Validating here turns that into a typed
        # JSON answer: bad policy → 503 with the actionable PolicyError
        # text; project deleted after mount → 404 (closing the
        # "deleted project keeps serving" gap noted at module top); registry
        # unreachable → 503 within _PREFLIGHT_TIMEOUT_S instead of a 46s
        # hang. Cost: one policy read per request — the same read the
        # Console API does per request, accepted for a correct answer.
        refusal = await self._preflight(project)
        if refusal is not None:
            status, payload = refusal
            if status == 404:
                # the SoR no longer has the project — stop serving the
                # cached mount AND close its child lifespan promptly (the
                # per-child stop; a gateway-wide event would keep the
                # orphaned session manager alive until full shutdown)
                async with self._lock:
                    self._apps.pop(project, None)
                    child_stop = self._child_stops.pop(project, None)
                if child_stop is not None:
                    child_stop.set()
            await self._send_json(send, status, payload)
            return
        try:
            # the mount phase re-reads the registry (_project_exists) and
            # enters the child lifespan — BOTH under the same fast deadline
            # as the preflight (Codex #132 r1: Postgres stalling BETWEEN the
            # two reads would otherwise hang this request on the driver's
            # own much longer timeout, past the promised fast 503)
            with anyio.fail_after(_PREFLIGHT_TIMEOUT_S):
                app = await self._app_for(project)
        except TimeoutError:
            await self._send_json(
                send,
                503,
                {
                    "error": f"project {project!r} mount timed out — the registry "
                    "or the session manager stalled; retry shortly"
                },
            )
            return
        except Exception as exc:  # noqa: BLE001 — a mount failure must answer typed, not a raw 500
            await self._send_json(
                send,
                503,
                {
                    "error": f"project {project!r} failed to mount "
                    f"({type(exc).__name__}) — see the gateway log"
                },
            )
            return
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

    async def _preflight(self, project: str) -> tuple[int, dict[str, Any]] | None:
        """Registry + policy preflight for one request — None means
        servable; otherwise the (status, payload) refusal to send (MCP12).

        Distinguishes the four states the measured empty-stream response
        collapsed: config error (503 + the actionable PolicyError text),
        project deleted (404), registry unreachable (503, bounded fast), and
        healthy (proceed). The not-in-registry case is decided by a direct
        row lookup in the ERROR path only — never by matching PolicyError
        message text."""
        from core.registry import get_project

        assert self._engine is not None, "gateway lifespan not started"
        try:
            with anyio.fail_after(_PREFLIGHT_TIMEOUT_S):
                async with self._engine.connect() as conn:
                    try:
                        await load_runtime_config_from_registry(conn, project)
                        return None
                    # MetadataConfigError: a malformed metadata_exposure is
                    # the same "config not servable" answer as a bad policy
                    # (the composed loader rightly refuses it)
                    except (PolicyError, MetadataConfigError) as exc:
                        if await get_project(conn, project) is None:
                            return 404, {
                                "error": f"project {project!r} is not in the registry "
                                "(or not path-addressable)"
                            }
                        return 503, {
                            "error": f"project {project!r} is not servable — "
                            f"configuration error: {exc}"
                        }
        except TimeoutError:
            return 503, {
                "error": "registry unreachable (timed out) — the gateway could not "
                "read the project's query policy; check Postgres"
            }
        except STORE_CLIENT_ERRORS as exc:
            return 503, {
                "error": f"registry unreachable ({type(exc).__name__}) — the gateway "
                "could not read the project's query policy; check Postgres"
            }

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
                # waits for all of them to finish closing (evicted children
                # already had their own event set)
                for child_stop in list(self._child_stops.values()):
                    child_stop.set()
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
            child_stop = anyio.Event()

            async def host(*, task_status: TaskStatus[None]) -> None:
                # ONE task owns the child lifespan end to end (see __init__):
                # started() only fires after a successful enter, so a failing
                # child startup propagates to the mount request loud
                async with app.router.lifespan_context(app):
                    task_status.started()
                    await child_stop.wait()

            await self._tasks.start(host)
            self._apps[project] = app
            self._child_stops[project] = child_stop
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
