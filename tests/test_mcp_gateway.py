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
from tests.conftest import DEMO_QUERY_POLICY


class _ChildApp:
    """A fake per-project ASGI app recording scopes + lifespan state."""

    def __init__(self, project: str) -> None:
        self.project = project
        self.scopes: list[dict[str, Any]] = []
        self.lifespan_entered = False
        self.lifespan_closed = False
        self.shared_closed = False
        self.deletes: list[Any] = []  # DELETE session-ids the child received (r7 teardown)
        self.closed_event = anyio.Event()
        # the session-manager pre-seed (MCP17) constructs a REAL manager
        # against these — the fake carries what the constructor reads
        self._mcp_server = SimpleNamespace(name=f"graphrag-{project}")
        self._session_manager: Any = None
        self.settings = SimpleNamespace(
            streamable_http_path="/mcp",
            json_response=False,
            stateless_http=False,
            transport_security=None,
        )
        self.router = SimpleNamespace(lifespan_context=self._lifespan)

    #: set to make this child's shared-bundle close RAISE — the gateway must
    #: contain it (one project's close error must never cancel the others)
    close_raises = False

    async def _graphrag_close_shared(self) -> None:
        # MCP16: the gateway host must close the shared client bundle once
        # the child app is done (eviction or shutdown) — sessions never do
        self.shared_closed = True
        if self.close_raises:
            raise RuntimeError("store unhealthy at close")

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
            child.closed_event.set()

        return ctx()

    def streamable_http_app(self) -> Any:
        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            self.scopes.append(scope)
            if scope.get("method") == "DELETE":
                hdrs = dict(scope.get("headers", []))
                self.deletes.append(hdrs.get(b"mcp-session-id"))
                # like the SDK's rebinding guard: reject a Host-less DELETE
                status = 200 if b"host" in hdrs else 421
                await send({"type": "http.response.start", "status": status, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            # like the SDK: an initialize response MINTS the session id in
            # its headers (the gateway intercepts it to stamp creation)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"mcp-session-id", f"minted-{self.project}".encode())],
                }
            )
            await send({"type": "http.response.body", "body": b"ok:" + self.project.encode()})

        app.router = self.router  # type: ignore[attr-defined]
        return app


class _Harness:
    def __init__(self, registry: set[str]) -> None:
        self.registry = registry
        self.children: dict[str, _ChildApp] = {}
        self.build_calls: list[str] = []
        # per-project config override — default: a VALID policy, so the
        # MCP12 per-request preflight passes and routing behaves as before
        self.configs: dict[str, dict[str, Any]] = {}

    def fake_build_server(self, project: str) -> _ChildApp:
        self.build_calls.append(project)
        child = _ChildApp(project)
        self.children[project] = child
        return child

    async def fake_get_project(self, conn: object, name: str) -> object | None:
        if name not in self.registry:
            return None
        config = self.configs.get(name, {"query_policy": DEMO_QUERY_POLICY})
        return SimpleNamespace(name=name, config=config)


async def _request(
    app: Any,
    path: str,
    raw_path: bytes | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    method: str = "POST",
) -> tuple[int, bytes]:
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def receive() -> dict[str, Any]:  # pragma: no cover — no request body reads
        return {"type": "http.request", "body": b""}

    scope = {
        "type": "http",
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode("utf-8"),
        "method": method,
        "headers": headers or [],
    }
    await app(scope, receive, send)
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
            # startup.complete is uvicorn's green light to dispatch requests:
            # AT THE SEND INSTANT the child task group must already exist, or
            # the first /mcp/<project> 500s on the unassigned group (Codex
            # #93 R1). Asserted INSIDE send — an after-the-fact check races
            # the lifespan task's next step and false-greens (probe-proven).
            assert app._tasks is not None  # noqa: SLF001
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

        # outside /mcp → 404 (except /health, asserted above — MCP17)
        assert (await _request(app, "/other"))[0] == 404

        # /health: a minimal LIVENESS surface (MCP17 — previously every
        # non-/mcp path 404'd). NON-SENSITIVE (Codex #137 r11): it bypasses
        # the SDK's DNS-rebinding check, so it must not leak mount names.
        status, body = await _request(app, "/health")
        assert status == 200 and b'"status": "ok"' in body
        assert b"mounted_projects" not in body  # no project-name enumeration

        # Codex #137 r4: the pre-seeded manager's reap timeout MUST equal the
        # gateway's captured _session_idle_timeout_s (the same value the
        # compaction horizon uses) — reading get_settings() fresh at mount
        # would let a runtime env change split reaping from compaction. Set
        # a DISTINCT captured value before the mount to discriminate.
        app._session_idle_timeout_s = 999.0  # noqa: SLF001

        # known project → routed; child sees root-relative path + prefix root_path
        status, body = await _request(app, "/mcp/nmmst")
        assert status == 200 and body == b"ok:nmmst"
        child = harness.children["nmmst"]
        assert child.lifespan_entered
        # MCP17: the pre-seeded session manager carries the idle timeout the
        # SDK supports but FastMCP never passes (sessions previously lived
        # until client DELETE or gateway restart — a stale DR-012 policy
        # snapshot could survive forever)
        assert child._session_manager is not None  # noqa: SLF001
        # ...and it is the GATEWAY's captured value, not a fresh settings read
        assert child._session_manager.session_idle_timeout == 999.0  # noqa: SLF001
        status, body = await _request(app, "/health")
        assert status == 200 and b'"status": "ok"' in body  # liveness only, no mount leak
        assert b"nmmst" not in body
        assert child.scopes[0]["path"] == "/"
        assert child.scopes[0]["root_path"] == "/mcp/nmmst"
        # the child's own streamable path was re-rooted so the gateway prefix
        # owns the URL (no /mcp/nmmst/mcp double-segment)
        assert child.settings.streamable_http_path == "/"

        # second request reuses the cached instance — §9 one-server-per-project
        await _request(app, "/mcp/nmmst/sub")
        assert harness.build_calls == ["nmmst"]
        assert child.scopes[1]["path"] == "/sub"

        # an ENCODED slash must not smuggle a segment boundary: /mcp/a%2Fb
        # decodes to project "a/b" (not path-addressable → 404), NEVER to
        # project "a" with child path /b — even when "a" exists (Codex #93
        # R3: wrong-project serving). The literal /mcp/nmmst/b above already
        # pins that a REAL sub-path still routes.
        harness.registry.add("a")
        status, body = await _request(app, "/mcp/a/b", raw_path=b"/mcp/a%2Fb")
        assert status == 404, body
        assert "a" not in harness.children  # the wrong project was never mounted

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


async def test_preflight_answers_typed_for_every_unservable_state(harness: _Harness) -> None:
    """MCP12: a session-lifespan failure used to surface as HTTP 200 + a
    ZERO-BYTE stream — the PolicyError's actionable text never reached the
    wire, an agent could not tell config-error from deleted from gateway
    crash from network flap (and with Postgres down the hang ran 46s, past
    every §21 budget). The per-request preflight splits those states into
    typed JSON answers: bad policy → 503 WITH the actionable message; a
    project DELETED after mount → 404 and the cached mount stops serving;
    registry unreachable → 503 fast; a mount failure → 503 naming the class,
    never a raw 500 traceback."""
    app = build_gateway()

    started = anyio.Event()
    finish = anyio.Event()

    async def lifespan_receive() -> dict[str, Any]:
        if not started.is_set():
            return {"type": "lifespan.startup"}
        await finish.wait()
        return {"type": "lifespan.shutdown"}

    async def lifespan_send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan"}, lifespan_receive, lifespan_send)
        await started.wait()

        # (1) config error: the actionable PolicyError text reaches the wire
        harness.registry.add("misconfigured")
        harness.configs["misconfigured"] = {}  # no query_policy block
        status, body = await _request(app, "/mcp/misconfigured")
        assert status == 503
        assert b"no query_policy block" in body  # the operator-actionable cause
        assert "misconfigured" not in harness.children  # never mounted

        # (1b) DR-012 snapshot: an ESTABLISHED session (mcp-session-id
        # header) is NOT config-revalidated — a config broken MID-session
        # must not 503 a healthy session whose lifespan policy snapshot is
        # authoritative (Codex #132 r2); only the NEXT session sees the fix
        session_headers = [(b"mcp-session-id", b"abc123")]
        status, _ = await _request(app, "/mcp/nmmst")  # mount while valid
        assert status == 200
        harness.configs["nmmst"] = {}  # broken AFTER the session started
        status, body = await _request(app, "/mcp/nmmst", headers=session_headers)
        assert status == 200, body  # the established session keeps serving
        status, _ = await _request(app, "/mcp/nmmst")  # a NEW session is refused
        assert status == 503
        del harness.configs["nmmst"]  # back to valid for the cases below

        # (2) deleted AFTER mount: the cached mount stops serving (the
        # module-top "deleted project keeps serving" gap, closed) AND the
        # child lifespan closes PROMPTLY — not at gateway shutdown (Codex
        # #132 r1: a gateway-wide stop event accumulated orphaned session
        # managers across delete/recreate cycles)
        status, _ = await _request(app, "/mcp/nmmst")
        assert status == 200 and "nmmst" in harness.children
        harness.registry.discard("nmmst")
        status, body = await _request(app, "/mcp/nmmst")
        assert status == 404, body
        evicted = harness.children["nmmst"]
        with anyio.fail_after(2):
            await evicted.closed_event.wait()
        assert evicted.lifespan_closed
        assert evicted.shared_closed  # MCP16: the host released the client bundle

        # a close that RAISES must be CONTAINED (gate-2 blocker): an
        # exception through the host's finally would make anyio cancel the
        # whole task group — one project's close error tearing down every
        # OTHER project's host. Evict a close-raising child and prove the
        # gateway keeps serving an unrelated, already-mounted neighbor.
        harness.registry.update({"fragile", "steady"})
        assert (await _request(app, "/mcp/steady"))[0] == 200
        assert (await _request(app, "/mcp/fragile"))[0] == 200
        harness.children["fragile"].close_raises = True
        harness.registry.discard("fragile")
        status, _ = await _request(app, "/mcp/fragile")
        assert status == 404
        with anyio.fail_after(2):
            await harness.children["fragile"].closed_event.wait()
        # the gateway survived — the neighbor still serves
        status, body = await _request(app, "/mcp/steady")
        assert status == 200 and body == b"ok:steady"

        # ...and a RECREATED project mounts a FRESH child, not the orphan
        harness.registry.add("nmmst")
        status, _ = await _request(app, "/mcp/nmmst")
        assert status == 200
        assert harness.children["nmmst"] is not evicted

        # deletion is a LIFECYCLE end, not a config edit: an established
        # session's request also answers 404 (the DR-012 snapshot shields
        # config edits only — a deleted project stops serving everyone)
        harness.registry.discard("nmmst")
        status, _ = await _request(app, "/mcp/nmmst", headers=[(b"mcp-session-id", b"abc123")])
        assert status == 404
        harness.registry.add("nmmst")

        # (3) a mount failure answers typed, never a raw 500 traceback
        harness.registry.add("brokenmount")

        def _boom(project: str) -> Any:
            raise RuntimeError("SDK exploded")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gateway_module, "build_server", _boom)
            status, body = await _request(app, "/mcp/brokenmount")
        assert status == 503
        assert b"RuntimeError" in body and b"Traceback" not in body

        # (4) the MOUNT phase is deadline-bounded too (Codex #132 r1):
        # Postgres stalling BETWEEN preflight and _project_exists must not
        # hang the request on the driver's own longer timeout
        harness.registry.add("stallmount")

        async def _stalling_exists(project: str) -> bool:
            await anyio.sleep(30)
            return True  # pragma: no cover — the deadline cuts first

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app, "_project_exists", _stalling_exists)
            import time as _time

            before = _time.monotonic()
            status, body = await _request(app, "/mcp/stallmount")
            assert _time.monotonic() - before < 10
        assert status == 503
        assert b"mount timed out" in body

        finish.set()


async def test_preflight_registry_outage_fails_fast_and_typed(
    monkeypatch: pytest.MonkeyPatch, harness: _Harness
) -> None:
    """MCP12: with Postgres down the old path hung 46s and then produced the
    empty-stream non-answer. The preflight turns it into a bounded, typed
    503 naming the outage — measured here as the raw builtin
    ConnectionRefusedError a dead Postgres actually raises at connect."""
    import time

    class _DeadConnect:
        async def __aenter__(self) -> object:
            raise ConnectionRefusedError("[Errno 111] Connection refused")

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        gateway_module,
        "create_async_engine",
        lambda *a, **k: SimpleNamespace(dispose=_async_noop, connect=_DeadConnect),
    )
    app = build_gateway()

    started = anyio.Event()
    finish = anyio.Event()

    async def lifespan_receive() -> dict[str, Any]:
        if not started.is_set():
            return {"type": "lifespan.startup"}
        await finish.wait()
        return {"type": "lifespan.shutdown"}

    async def lifespan_send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan"}, lifespan_receive, lifespan_send)
        await started.wait()
        before = time.monotonic()
        status, body = await _request(app, "/mcp/nmmst")
        assert time.monotonic() - before < 5.0  # bounded, not the 46s hang
        assert status == 503
        assert b"registry unreachable" in body and b"ConnectionRefusedError" in body
        finish.set()


async def test_established_sessions_pass_through_a_registry_outage(
    monkeypatch: pytest.MonkeyPatch, harness: _Harness
) -> None:
    """MCP12 r2 companion pin: during a registry outage an ESTABLISHED
    session (already mounted, mcp-session-id header) must PROCEED to its
    child and degrade IN-protocol (the tools' typed STORE_UNAVAILABLE) —
    an out-of-protocol gateway 503 here is exactly what Codex #132 r2
    flagged; a regression to unconditional preflight reintroduces it
    silently."""
    app = build_gateway()

    started = anyio.Event()
    finish = anyio.Event()

    async def lifespan_receive() -> dict[str, Any]:
        if not started.is_set():
            return {"type": "lifespan.startup"}
        await finish.wait()
        return {"type": "lifespan.shutdown"}

    async def lifespan_send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan"}, lifespan_receive, lifespan_send)
        await started.wait()

        # mount while the registry is healthy, session establishes
        status, _ = await _request(app, "/mcp/nmmst")
        assert status == 200

        # registry dies AFTER the mount
        class _DeadConnect:
            async def __aenter__(self) -> object:
                raise ConnectionRefusedError("[Errno 111] Connection refused")

            async def __aexit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(
            app, "_engine", SimpleNamespace(dispose=_async_noop, connect=_DeadConnect)
        )

        # the established session still reaches its child, in-protocol
        status, body = await _request(app, "/mcp/nmmst", headers=[(b"mcp-session-id", b"abc123")])
        assert status == 200 and body == b"ok:nmmst"

        # a NEW session (no header) is still refused fast and typed
        status, body = await _request(app, "/mcp/nmmst")
        assert status == 503 and b"registry unreachable" in body

        finish.set()


async def test_sessions_expire_at_the_absolute_age_ceiling(harness: _Harness) -> None:
    """MCP17 (Codex #137 r1): the SDK idle timeout RESETS on every request,
    so a chatty session's DR-012 policy snapshot never expires — a policy
    tightening could stay invisible forever. The gateway enforces an
    ABSOLUTE ceiling: past it the session's requests answer 404 and the
    client re-initializes, picking up the CURRENT policy."""
    app = build_gateway()
    started = anyio.Event()
    finish = anyio.Event()

    async def lifespan_receive() -> dict[str, Any]:
        if not started.is_set():
            return {"type": "lifespan.startup"}
        await finish.wait()
        return {"type": "lifespan.shutdown"}

    async def lifespan_send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan"}, lifespan_receive, lifespan_send)
        await started.wait()

        # only ids CAPTURED from a successful initialize are tracked
        # (Codex #137 r3 P1: an unknown id must NOT be inserted, or an
        # unauthenticated flood of unique ids grows the ledger unbounded) —
        # simulate the initialize capture, then use the id
        app._record_session_start(b"chatty-1")  # noqa: SLF001
        sid = [(b"mcp-session-id", b"chatty-1")]
        status, _ = await _request(app, "/mcp/nmmst", headers=sid)
        assert status == 200  # young session serves

        # an UNKNOWN id (never initialized here) is NOT tracked — it passes
        # through to the child (which owns session-not-found), and the
        # ledger does not grow from attacker-supplied ids
        before = len(app._session_seen)  # noqa: SLF001
        status, _ = await _request(app, "/mcp/nmmst", headers=[(b"mcp-session-id", b"forged-xyz")])
        assert status == 200  # passed through to the (fake) child
        assert b"forged-xyz" not in app._session_seen  # noqa: SLF001
        assert len(app._session_seen) == before  # untracked, no growth  # noqa: SLF001

        # backdate first-seen past the ceiling: the NEXT request must 404
        # with the actionable re-initialize message — and the ledger entry
        # is KEPT, so a client that ignores the 404 and re-sends the SAME
        # id stays refused (popping would restart its clock, making the
        # ceiling depend on client compliance — gate-2)
        first, last = app._session_seen[b"chatty-1"]  # noqa: SLF001
        app._session_seen[b"chatty-1"] = (first - app._session_max_age_s - 1, last)  # noqa: SLF001
        sid_host = [(b"mcp-session-id", b"chatty-1"), (b"host", b"127.0.0.1:8300")]
        status, body = await _request(app, "/mcp/nmmst", headers=sid_host)
        assert status == 404 and b"TERMINATED" in body  # honest wording (r5): not "refresh"
        # Codex #137 r7/r8: the ceiling RELEASES the server session — an
        # internal DELETE reached the child (its SDK task + lifespan torn
        # down, not left until idle), and it carried the ORIGINAL Host so
        # the transport's DNS-rebinding guard accepts it (a Host-less DELETE
        # 421s → the entry would wrongly stay/drop). Confirmed teardown pops.
        child = harness.children["nmmst"]
        assert b"chatty-1" in child.deletes
        assert b"host" in dict(child.scopes[-1]["headers"])  # Host carried into the DELETE
        assert b"chatty-1" not in app._session_seen  # noqa: SLF001 — confirmed teardown popped it
        # Codex #137 r12: the SDK manager's OWN registries are evicted too —
        # the pinned SDK never pops an explicitly-DELETEd transport, so
        # without this each rollover leaks one _server_instances entry. The
        # attribute-name tripwire (a real manager was pre-seeded in _app_for)
        # fails red on an SDK rename rather than silently leaking again.
        mgr = app._session_managers["nmmst"]  # noqa: SLF001
        assert isinstance(mgr._server_instances, dict)  # noqa: SLF001 — SDK attr present
        assert isinstance(mgr._session_owners, dict)  # noqa: SLF001
        # a re-sent id is now unknown → passes through to the child's own 404
        status, _ = await _request(app, "/mcp/nmmst", headers=sid_host)
        assert status == 200  # the fake child accepts it; the gateway does not re-track it
        assert b"chatty-1" not in app._session_seen  # noqa: SLF001

        # Codex #137 r8: a DELETE the transport REJECTS (no confirmed
        # teardown) must KEEP the ledger entry — else the still-live session
        # escapes the bound via the unknown-id pass-through. Age a session
        # and send WITHOUT Host so the fake guard 421s the internal DELETE.
        app._record_session_start(b"rebind-1")  # noqa: SLF001
        f, la = app._session_seen[b"rebind-1"]  # noqa: SLF001
        app._session_seen[b"rebind-1"] = (f - app._session_max_age_s - 1, la)  # noqa: SLF001
        status, _ = await _request(app, "/mcp/nmmst", headers=[(b"mcp-session-id", b"rebind-1")])
        assert status == 404  # still refused
        assert b"rebind-1" in app._session_seen  # noqa: SLF001 — KEPT (teardown not confirmed)

        # Codex #137 r2 P1: compaction must NOT forget a LIVE (still-touched,
        # not-yet-over-age) session under a dead flood. Establish a young
        # tracked session, flood the ledger with certainly-reaped entries,
        # touch the young one — it survives, the dead flood is forgotten.
        import time as _t

        app._record_session_start(b"young-1")  # noqa: SLF001
        young = [(b"mcp-session-id", b"young-1")]
        stale = _t.monotonic() - app._session_idle_timeout_s - 120  # noqa: SLF001
        for i in range(4100):
            app._session_seen[f"dead-{i}".encode()] = (stale, stale)  # noqa: SLF001
        status, _ = await _request(app, "/mcp/nmmst", headers=young)  # touch → compaction
        assert status == 200  # young session still serves
        assert b"young-1" in app._session_seen  # noqa: SLF001 — live entry survived
        assert len(app._session_seen) < 100  # the dead flood was forgotten  # noqa: SLF001

        # Codex #137 r2 P2: the clock starts at CREATION — the id minted in
        # the initialize RESPONSE is stamped immediately, not on first use
        status, _ = await _request(app, "/mcp/nmmst")  # initializing (no header)
        assert status == 200
        assert b"minted-nmmst" in app._session_seen  # noqa: SLF001

        # Codex #137 r5 P1: a gateway that ONLY ever sees initializations
        # (one-shot clients that never follow up) must still stay bounded —
        # _record_session_start compacts on insert. Flood with reaped
        # (idle-past-horizon) entries, then one more initialize must trim.
        stale = _t.monotonic() - app._session_idle_timeout_s - 120  # noqa: SLF001
        for i in range(4100):
            app._session_seen[f"reaped-{i}".encode()] = (stale, stale)  # noqa: SLF001
        app._record_session_start(b"fresh-init")  # noqa: SLF001 — insert compacts
        assert len(app._session_seen) < 100  # the dead flood was forgotten  # noqa: SLF001
        assert b"fresh-init" in app._session_seen  # noqa: SLF001 — the fresh one survives

        # Codex #137 r9: a LARGE LIVE set (nothing to reap) must NOT rebuild
        # the dict on every request — the high-water mark rises to 2x the
        # post-compaction size, so compaction does not re-run until the
        # ledger grows again. Fill with live (recent) entries past the mark
        # and confirm the mark advanced beyond the current size.
        live = _t.monotonic()
        for i in range(5000):
            app._session_seen[f"live-{i}".encode()] = (live, live)  # noqa: SLF001
        app._compact_sessions(live)  # noqa: SLF001 — one compaction over live entries
        assert app._compact_at >= 2 * len(app._session_seen) - 1  # noqa: SLF001 — mark raised
        assert app._compact_at > len(app._session_seen)  # noqa: SLF001 — no immediate re-trigger

        # Codex #137 r10: after a spike raises the mark, sessions time out,
        # and the ledger sits BELOW the raised mark — the size trigger never
        # fires again. A TIME trigger (once per reap horizon) must still
        # compact so the mark FALLS back and reaped memory is reclaimed.
        raised_mark = app._compact_at  # noqa: SLF001 — the post-spike high mark
        assert raised_mark > 4096
        # the whole live set is now reaped (last activity beyond the horizon)
        # and the last compaction was more than one horizon ago
        old = live - app._session_idle_timeout_s - 120  # noqa: SLF001
        app._session_seen = {sid: (f, old) for sid, (f, _) in app._session_seen.items()}  # noqa: SLF001
        app._last_compact_at = live - app._session_idle_timeout_s - 120  # noqa: SLF001
        app._compact_sessions(live + 1)  # noqa: SLF001 — below the mark, but past the horizon
        assert app._session_seen == {}  # noqa: SLF001 — reaped entries cleared by the time trigger
        assert app._compact_at == 4096  # noqa: SLF001 — mark fell back

        # Codex #137 r6: an explicit MCP session termination (DELETE) drops
        # the ledger entry IMMEDIATELY on a 2xx — a high-churn client that
        # creates+DELETEs many short-lived sessions cannot grow the ledger
        # within the idle reap window
        app._record_session_start(b"short-lived")  # noqa: SLF001
        assert b"short-lived" in app._session_seen  # noqa: SLF001
        status, _ = await _request(
            app,
            "/mcp/nmmst",
            headers=[(b"mcp-session-id", b"short-lived"), (b"host", b"127.0.0.1:8300")],
            method="DELETE",
        )
        assert status == 200  # the (fake) child accepted the termination
        assert b"short-lived" not in app._session_seen  # noqa: SLF001 — dropped at once

        finish.set()


async def test_confirmed_teardown_evicts_the_sdk_manager_registries(harness: _Harness) -> None:
    """Codex #137 r12: the pinned SDK's StreamableHTTPSessionManager does
    NOT remove an explicitly-DELETEd transport from its own
    _server_instances/_session_owners maps (only the idle path pops them),
    so a confirmed max-age teardown must evict the id from the manager too —
    else a busy long-running gateway leaks one SDK entry per rollover."""
    app = build_gateway()
    started = anyio.Event()
    finish = anyio.Event()

    async def lifespan_receive() -> dict[str, Any]:
        if not started.is_set():
            return {"type": "lifespan.startup"}
        await finish.wait()
        return {"type": "lifespan.shutdown"}

    async def lifespan_send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan"}, lifespan_receive, lifespan_send)
        await started.wait()
        await _request(app, "/mcp/nmmst")  # mount → pre-seed the manager
        mgr = app._session_managers["nmmst"]  # noqa: SLF001
        mgr._server_instances["s-1"] = object()  # noqa: SLF001 — SDK-tracked transport
        mgr._session_owners["s-1"] = object()  # noqa: SLF001 — and its owner metadata

        app._evict_from_manager("nmmst", b"s-1")  # noqa: SLF001 — confirmed-teardown eviction
        assert "s-1" not in mgr._server_instances  # noqa: SLF001
        assert "s-1" not in mgr._session_owners  # noqa: SLF001
        # unknown project / id is a no-op, never a crash
        app._evict_from_manager("nmmst", b"never")  # noqa: SLF001
        app._evict_from_manager("ghost", b"s-1")  # noqa: SLF001
        finish.set()


def test_create_app_is_a_uvicorn_factory() -> None:
    """MCP17: `uvicorn.run(app_instance)` made `workers` unusable (each
    worker process must IMPORT the app) — the gateway was structurally
    single-process. The import-string factory is the multi-process escape;
    each call builds a fresh, independent gateway."""
    from core.mcp.gateway import McpGateway, create_app

    a, b = create_app(), create_app()
    assert isinstance(a, McpGateway) and isinstance(b, McpGateway)
    assert a is not b  # one per worker process, no shared instance
