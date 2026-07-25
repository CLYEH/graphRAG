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

import contextlib
import json
import re
import time
from typing import Any
from urllib.parse import unquote

import anyio
from anyio.abc import TaskGroup, TaskStatus
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
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
        # MCP17 (Codex #137 r1): ABSOLUTE session age. The SDK's idle timeout
        # resets on every request, so a chatty session's DR-012 policy
        # snapshot would never expire — this gateway-side ledger records when
        # a session id was FIRST seen (>= its true start, so the bound errs
        # tight) and answers 404 past the ceiling; the client re-initializes
        # and the fresh lifespan reads the CURRENT policy.
        # sid -> (first_seen, last_seen). first_seen drives the ceiling;
        # last_seen drives SAFE compaction (Codex #137 r2 P1: an entry may
        # be forgotten only once the SDK has necessarily reaped the session
        # - idle_timeout past its last activity - because a forgotten id
        # that the child still serves would restart its clock and resume
        # the stale snapshot; a forgotten DEAD id is harmless, the child
        # answers "session not found" itself).
        self._session_seen: dict[bytes, tuple[float, float]] = {}
        self._session_max_age_s = 7200.0
        self._session_idle_timeout_s = 1800.0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"unsupported ASGI scope type {scope['type']!r}")
        raw_path = scope.get("raw_path") or scope["path"].encode("utf-8")
        # MCP17: a minimal ops surface — /health answers 200 with the
        # mounted projects (previously /, /mcp and /health all 404'd, so an
        # operator could not even tell the gateway was alive or what it had
        # mounted). Read-only; sessions stay SDK-internal.
        if scope["path"] == "/health":
            await self._send_json(
                send, 200, {"status": "ok", "mounted_projects": sorted(self._apps)}
            )
            return
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
        # MCP12 preflight: the child server reads its policy per SESSION
        # lifespan, and a failure there surfaces as HTTP 200 + an empty
        # stream (the SDK gives the error no wire shape), which an agent
        # cannot distinguish from a gateway crash or a network flap. The
        # CONFIG validation runs only for session INITIALIZATION (no
        # mcp-session-id header yet — the request that would hit the broken
        # lifespan): bad policy → 503 with the actionable PolicyError text;
        # registry unreachable → 503 within _PREFLIGHT_TIMEOUT_S instead of
        # a 46s hang. ESTABLISHED sessions keep their lifespan policy
        # snapshot (DR-012: config edits apply to the NEXT session — Codex
        # #132 r2), so they get only the cheap existence check (project
        # DELETED after mount → 404 + eviction; deletion is a lifecycle end,
        # not a config edit), and during a registry outage they pass through
        # to their own IN-PROTOCOL typed degradation.
        headers = scope.get("headers") or []
        session_id = next((v for k, v in headers if k.lower() == b"mcp-session-id"), None)
        initializing = session_id is None
        if session_id is not None:
            expired = self._touch_session_age(session_id)
            if expired:
                await self._send_json(
                    send,
                    404,
                    {
                        "error": "session TERMINATED — it exceeded the maximum age "
                        f"({int(self._session_max_age_s)}s). Open a NEW transport and "
                        "re-initialize to continue with the project's CURRENT policy "
                        "(the same 404-then-reconnect contract as the SDK's idle "
                        "reaper — a client that keeps the old session id on a fresh "
                        "transport stays terminated)."
                    },
                )
                return
        refusal = await self._preflight(project, initializing=initializing)
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
        if initializing:
            # Codex #137 r2 P2: the absolute clock starts at CREATION — the
            # id is minted in the initialize RESPONSE headers, so intercept
            # them (a clock started at first follow-up could be delayed
            # almost a full idle_timeout, stretching the ceiling)
            async def send_capturing(message: dict[str, Any]) -> None:
                if message.get("type") == "http.response.start":
                    for key, value in message.get("headers", []):
                        if key.lower() == b"mcp-session-id":
                            self._record_session_start(bytes(value))
                await send(message)

            await app(child_scope, receive, send_capturing)
            return
        await app(child_scope, receive, send)

    def _compact_sessions(self, now: float) -> None:
        """Forget ids whose sessions the SDK has CERTAINLY reaped
        (idle_timeout + margin since their LAST activity) once the ledger is
        large. A live session keeps refreshing its last-seen — even while
        being refused past the ceiling — so its tombstone survives and the
        age bound holds by construction; only genuinely-dead ids are
        dropped, and a request under a dropped (reaped) id then takes the
        unknown-id pass-through to the child's own 404. Runs from BOTH
        insertion and touch (Codex #137 r5 P1: one-shot init clients never
        touch, so insert-time compaction is what bounds a gateway that only
        ever sees initializations)."""
        if len(self._session_seen) <= 4096:
            return
        horizon = now - (self._session_idle_timeout_s + 60.0)
        self._session_seen = {
            sid: (f, s) for sid, (f, s) in self._session_seen.items() if s > horizon
        }

    def _record_session_start(self, session_id: bytes) -> None:
        """Stamp a session's creation (Codex #137 r2 P2): the id is captured
        from the INITIALIZE response, so the absolute clock starts at
        creation — not at the first follow-up request, which a client could
        delay to stretch the documented ceiling. Compacts on insert so a
        gateway that only ever sees initializations stays bounded (r5 P1)."""
        now = time.monotonic()
        self._session_seen.setdefault(session_id, (now, now))
        self._compact_sessions(now)

    def _touch_session_age(self, session_id: bytes) -> bool:
        """Inspect a KNOWN session id's age — True when past the absolute
        ceiling (MCP17: the idle timeout resets per request, so ONLY an
        absolute bound caps a chatty session's stale policy snapshot).

        Only ids CAPTURED from a successful initialize response are tracked
        (:meth:`_record_session_start`) — an UNKNOWN id is NOT inserted here
        (Codex #137 r3 P1: setdefault-ing every client-supplied id let an
        unauthenticated flood of unique ids grow the ledger without bound;
        an unknown id now passes through untracked to the SDK, which
        answers its own 404). A known entry is KEPT on expiry (an ignored
        404 must not restart the clock)."""
        now = time.monotonic()
        seen = self._session_seen.get(session_id)
        if seen is None:
            # untracked (never initialized through this gateway, or already
            # reaped): pass through — the child owns the session-not-found
            return False
        first, _last = seen
        self._session_seen[session_id] = (first, now)
        self._compact_sessions(now)  # before the expiry return: a refused touch must still compact
        return now - first > self._session_max_age_s

    async def _preflight(
        self, project: str, *, initializing: bool
    ) -> tuple[int, dict[str, Any]] | None:
        """Registry (+ config, when ``initializing``) preflight for one
        request — None means proceed; otherwise the (status, payload)
        refusal to send (MCP12).

        A session-INITIALIZATION request gets the full config validation
        (it is the one that would hit the broken child lifespan and get the
        measured empty-stream non-answer): config error → 503 + the
        actionable text, deleted → 404, registry unreachable → 503 bounded
        fast. An ESTABLISHED session request gets only the existence check
        (DR-012: its policy snapshot from lifespan start stays authoritative
        — a config edit mid-session must not 503 a healthy session), and a
        registry outage lets it PROCEED to its own in-protocol typed
        degradation rather than an out-of-protocol JSON error. The
        not-in-registry case is decided by a direct row lookup — never by
        matching PolicyError message text."""
        from core.registry import get_project

        assert self._engine is not None, "gateway lifespan not started"
        not_found = (
            404,
            {"error": f"project {project!r} is not in the registry (or not path-addressable)"},
        )
        try:
            with anyio.fail_after(_PREFLIGHT_TIMEOUT_S):
                async with self._engine.connect() as conn:
                    if not initializing:
                        if await get_project(conn, project) is None:
                            return not_found
                        return None
                    try:
                        await load_runtime_config_from_registry(conn, project)
                        return None
                    # MetadataConfigError: a malformed metadata_exposure is
                    # the same "config not servable" answer as a bad policy
                    # (the composed loader rightly refuses it)
                    except (PolicyError, MetadataConfigError) as exc:
                        if await get_project(conn, project) is None:
                            return not_found
                        return 503, {
                            "error": f"project {project!r} is not servable — "
                            f"configuration error: {exc}"
                        }
        except TimeoutError:
            if not initializing:
                return None  # the established session degrades in-protocol
            return 503, {
                "error": "registry unreachable (timed out) — the gateway could not "
                "read the project's query policy; check Postgres"
            }
        except STORE_CLIENT_ERRORS as exc:
            if not initializing:
                return None  # the established session degrades in-protocol
            return 503, {
                "error": f"registry unreachable ({type(exc).__name__}) — the gateway "
                "could not read the project's query policy; check Postgres"
            }

    async def _lifespan(self, receive: Any, send: Any) -> None:
        message = await receive()
        assert message["type"] == "lifespan.startup"
        try:
            settings = get_settings()
            self._session_max_age_s = float(settings.mcp_session_max_age_s)
            self._session_idle_timeout_s = float(settings.mcp_session_idle_timeout_s)
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
            # MCP17: the SDK's session manager supports session_idle_timeout
            # but FastMCP.streamable_http_app never passes it (its lazy init
            # omits the param → sessions lived until client DELETE or
            # gateway restart, so an abandoned agent-platform session — and
            # its DR-012 policy snapshot — could survive FOREVER). Pre-seed
            # the manager with the timeout; streamable_http_app reuses a
            # non-None _session_manager as-is.
            server._session_manager = StreamableHTTPSessionManager(  # noqa: SLF001
                app=server._mcp_server,  # noqa: SLF001
                json_response=server.settings.json_response,
                stateless=server.settings.stateless_http,
                security_settings=server.settings.transport_security,
                # the SAME lifetime the compaction horizon uses (Codex #137
                # r4): reading get_settings() fresh here would let a runtime
                # env change give a later-mounted manager a LONGER reap
                # timeout than _session_idle_timeout_s (captured at lifespan
                # start), so compaction could forget an id the SDK still
                # holds live — its next request then takes the unknown-id
                # pass-through and permanently escapes the age ceiling
                session_idle_timeout=self._session_idle_timeout_s,
            )
            app = server.streamable_http_app()
            assert self._tasks is not None, "gateway lifespan not started"
            child_stop = anyio.Event()

            async def host(*, task_status: TaskStatus[None]) -> None:
                # ONE task owns the child lifespan end to end (see __init__):
                # started() only fires after a successful enter, so a failing
                # child startup propagates to the mount request loud
                try:
                    async with app.router.lifespan_context(app):
                        task_status.started()
                        await child_stop.wait()
                finally:
                    # MCP16: sessions share one client bundle and no longer
                    # close it — the host (this child's owner) closes it once
                    # the child app is done (eviction or gateway shutdown).
                    # CONTAINED (gate-2 blocker): an exception escaping this
                    # finally would make anyio cancel the task group — one
                    # project's close error tearing down EVERY project's
                    # host, the exact isolation the gateway exists to
                    # provide. Cancellation still propagates.
                    closer = getattr(server, "_graphrag_close_shared", None)
                    if closer is not None:
                        with contextlib.suppress(Exception):  # never a cascade
                            await closer()

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


def create_app() -> McpGateway:
    """Uvicorn FACTORY entrypoint (MCP17): ``uvicorn.run("core.mcp.gateway:
    create_app", factory=True, workers=N)``. Passing an already-instantiated
    app object made uvicorn's ``workers`` unusable (it must import the app in
    each worker process) — the gateway was structurally single-process with
    no multi-process escape. Each worker builds its OWN gateway (own mounts,
    own sessions); streamable-HTTP sessions are process-sticky, so workers>1
    needs session-affinity in front (the CLI says so loudly)."""
    return build_gateway()
