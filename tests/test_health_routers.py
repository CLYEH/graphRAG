"""Why: BA7's routers are thin projections over ONE producer
(core.observability.health) — these tests pin the HTTP orchestration: the
frozen payload passthrough, meta.build_id naming the build the payload is
ABOUT, the 404 gate, and the deliberate ABSENCE of the query surface's 409
(an observation surface reports on bootstrap/broken states — the
"precedence belongs to the concept" lesson cuts both ways). The report
semantics themselves (§19 precedence, §20 comparability) are core's, tested
in test_observability_health.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import db_conn
from core.observability.health import HealthReport

pytestmark = pytest.mark.contract

_BUILD = uuid.uuid4()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = create_app()

    async def _conn() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[db_conn] = _conn
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"api.routers.health.{name}", fn)


def _project_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_project(conn: Any, name: str) -> Any:
        return SimpleNamespace(name=name)

    _stub(monkeypatch, "get_project", fake_get_project)


def _report(**over: Any) -> HealthReport:
    base: dict[str, Any] = {
        "project": "p",
        "status": "Healthy",
        "active_build_id": _BUILD,
        "drift": (),
        "metrics": {"pending_review": 0},
    }
    base.update(over)
    return HealthReport(**base)


def test_health_serves_the_frozen_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: the router speaks the FROZEN HealthReport (to_payload) — the
    # lower-snake enum, drift object-or-null, warnings typed — and meta names
    # the active build the counts are scoped to.
    _project_exists(monkeypatch)
    report = _report(
        status="Needs review",
        drift=("graph drift: postgres has 2 entities, neo4j 1",),
        metrics={"pending_review": 3, "documents": 5, "active_build": str(_BUILD)},
        warnings=("drift check unavailable: Neo4jError",),
    )

    async def fake_report(conn: Any, project: str, **providers: Any) -> HealthReport:
        return report

    _stub(monkeypatch, "health_report", fake_report)
    r = client.get("/projects/p/health")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "needs_review"  # the frozen enum, never the display string
    assert data["pending_review"] == 3 and data["counts"]["documents"] == 5
    assert data["drift"] == {"failures": ["graph drift: postgres has 2 entities, neo4j 1"]}
    assert data["warnings"] == [
        {"code": "STORE_UNAVAILABLE", "message": "drift check unavailable: Neo4jError"}
    ]
    assert r.json()["meta"]["build_id"] == str(_BUILD)


def test_metrics_reprojects_the_same_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (class 5): /metrics and /health must NEVER disagree — one producer,
    # two projections; the snapshot is the report's metrics dict verbatim.
    _project_exists(monkeypatch)
    calls: list[str] = []
    metrics = {"documents": 5, "pending_review": 2, "builds_total": 1}

    async def fake_report(conn: Any, project: str, **providers: Any) -> HealthReport:
        calls.append(project)
        return _report(metrics=metrics)

    _stub(monkeypatch, "health_report", fake_report)
    r = client.get("/projects/p/metrics")
    assert r.status_code == 200
    assert r.json()["data"] == metrics
    assert r.json()["meta"]["build_id"] == str(_BUILD)
    assert calls == ["p"]  # the §19 producer served it — no second bookkeeping


def test_bootstrap_is_a_report_never_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (the precedence lesson's dual): the query surface 409s without an
    # active build because it cannot SERVE; this surface OBSERVES — a
    # bootstrap project is a legitimate healthy report with null build ids,
    # and /eval serves the all-null report (measured facts only).
    _project_exists(monkeypatch)

    async def fake_report(conn: Any, project: str, **providers: Any) -> HealthReport:
        return _report(active_build_id=None)

    async def fake_eval(conn: Any, project: str) -> dict[str, Any]:
        return {"build_id": None, "passed": None, "regression": None, "metrics": {}}

    _stub(monkeypatch, "health_report", fake_report)
    _stub(monkeypatch, "latest_eval_payload", fake_eval)
    r = client.get("/projects/p/health")
    assert r.status_code == 200
    assert r.json()["data"]["active_build_id"] is None
    assert r.json()["meta"]["build_id"] is None
    r = client.get("/projects/p/eval")
    assert r.status_code == 200
    assert r.json()["data"]["build_id"] is None
    assert r.json()["meta"]["build_id"] is None


def test_eval_serves_the_latest_report(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _project_exists(monkeypatch)
    payload = {
        "build_id": str(_BUILD),
        "passed": True,
        "regression": False,
        "metrics": {"groundedness": 0.9},
    }

    async def fake_eval(conn: Any, project: str) -> dict[str, Any]:
        return payload

    _stub(monkeypatch, "latest_eval_payload", fake_eval)
    r = client.get("/projects/p/eval")
    assert r.status_code == 200
    assert r.json()["data"] == payload
    assert r.json()["meta"]["build_id"] == str(_BUILD)  # the build the report is ABOUT


def test_broken_store_config_cannot_poison_the_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY (Codex #62, the #53 R3 eager-acquisition class): the projection
    # stores are PROVIDERS invoked only when the drift probe runs — a missing
    # project must 404 even when Neo4j/Qdrant construction would raise.
    # Discriminating: the old shape resolved them as route dependencies, so
    # this request answered 500 before the 404.
    async def missing(conn: Any, name: str) -> None:
        return None

    def boom(request: Any) -> Any:
        raise ValueError("invalid store config")

    _stub(monkeypatch, "get_project", missing)
    _stub(monkeypatch, "qdrant_client", boom)
    _stub(monkeypatch, "neo4j_driver", boom)
    r = client.get("/projects/ghost/health")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"


@pytest.mark.parametrize("path", ["health", "metrics", "eval", "mcp"])
def test_unknown_project_is_404_on_every_surface(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    async def missing(conn: Any, name: str) -> None:
        return None

    async def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not run")

    _stub(monkeypatch, "get_project", missing)
    _stub(monkeypatch, "health_report", must_not_run)
    _stub(monkeypatch, "latest_eval_payload", must_not_run)
    r = client.get(f"/projects/ghost/{path}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_mcp_info_derives_the_url_from_the_gateway_settings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: the advertised URL must be the one the DR-012 gateway actually
    # serves — same settings (mcp_http_host/port), same path shape
    # (/mcp/<project>). Deriving it anywhere else would let the Console hand
    # an operator a URL that resolves nowhere the moment the gateway moves.
    _project_exists(monkeypatch)
    monkeypatch.setattr(
        "api.routers.health.get_settings",
        lambda: SimpleNamespace(mcp_http_host="10.0.0.7", mcp_http_port=9300, mcp_public_host=None),
    )
    r = client.get("/projects/p/mcp")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == {
        "transport": "streamable-http",
        "auth": "none",
        "url": "http://10.0.0.7:9300/mcp/p",
    }
    # the payload is about the CONNECTION surface, not any build's content
    assert body["meta"]["build_id"] is None


def test_mcp_info_percent_encodes_the_project_segment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: the emitted URL is a URL, not a display string — a name carrying
    # URL-significant characters (space, #, ?) must be percent-encoded or the
    # operator copies a link that truncates at the fragment/query or is
    # outright invalid. The gateway matches the RAW path and decodes per
    # segment (Codex #93 R3), so the encoded form is what it resolves.
    # (A name containing `/` cannot reach this route at all — the contract's
    # own "path-addressable only" claim, pinned by the sibling test below.)
    _project_exists(monkeypatch)
    monkeypatch.setattr(
        "api.routers.health.get_settings",
        lambda: SimpleNamespace(
            mcp_http_host="127.0.0.1", mcp_http_port=8300, mcp_public_host=None
        ),
    )
    r = client.get("/projects/my%20proj%23a/mcp")
    assert r.status_code == 200
    assert r.json()["data"]["url"] == "http://127.0.0.1:8300/mcp/my%20proj%23a"


def test_mcp_info_route_is_unreachable_for_non_path_addressable_names(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHY: the contract states `url` is ALWAYS non-null because the route is
    # path-keyed exactly like the gateway — a name with `/` reaches neither.
    # That guarantee is what lets McpInfo require a non-null url, so pin the
    # routing fact rather than trusting the prose.
    _project_exists(monkeypatch)
    assert client.get("/projects/a%2Fb/mcp").status_code == 404


@pytest.mark.parametrize(
    ("bind", "public", "expected_host"),
    [
        ("10.0.0.7", None, "10.0.0.7"),  # a dialable bind is advertised as-is
        ("0.0.0.0", None, "testserver"),  # wildcard → the host the Console reached
        ("::", None, "testserver"),  # the IPv6 wildcard is the same trap
        ("0.0.0.0", "mcp.example.lan", "mcp.example.lan"),  # operator's own answer wins
        ("::1", None, "[::1]"),  # IPv6 literal needs authority brackets
        ("0.0.0.0", "[::1]", "[::1]"),  # already-bracketed: bracketing is idempotent
        ("0:0:0:0:0:0:0:0", None, "testserver"),  # an unlisted SPELLING of the same wildcard
        ("::0.0.0.0", None, "testserver"),  # and the v4-mapped spelling
    ],
)
def test_mcp_info_advertises_a_dialable_host(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    bind: str,
    public: str | None,
    expected_host: str,
) -> None:
    """Why: a BIND is an interface, not an address (Codex #113 P1). Advertising
    `0.0.0.0` hands the operator a URL their agent resolves LOCALLY — it never
    reaches this gateway — and an unbracketed IPv6 literal makes `host:port`
    ambiguous, i.e. a malformed URL. The wildcard case is the likely one: the
    CLI warning tells operators to put a LAN bind into the SETTING, and
    `0.0.0.0` is how that is spelled.
    """
    _project_exists(monkeypatch)
    monkeypatch.setattr(
        "api.routers.health.get_settings",
        lambda: SimpleNamespace(mcp_http_host=bind, mcp_http_port=8300, mcp_public_host=public),
    )
    r = client.get("/projects/p/mcp")
    assert r.status_code == 200
    assert r.json()["data"]["url"] == f"http://{expected_host}:8300/mcp/p"


@pytest.mark.parametrize("bad", ["mcp.example:8443", "bad host", "[fe80::1%eth0]"])
def test_mcp_info_fails_loud_on_an_unusable_public_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Why: these values cannot appear in a valid URI authority, so answering
    200 would emit a `url` violating the frozen `format: uri` — the endpoint
    must instead fail as a typed INTERNAL whose message NAMES the setting, so
    the operator fixes mcp_public_host rather than debugging the gateway.
    """
    _project_exists(monkeypatch)
    monkeypatch.setattr(
        "api.routers.health.get_settings",
        lambda: SimpleNamespace(mcp_http_host="127.0.0.1", mcp_http_port=8300, mcp_public_host=bad),
    )
    r = client.get("/projects/p/mcp")
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "INTERNAL"
    assert "mcp_public_host" in err["message"]  # actionable naming, not a bare 500


@pytest.mark.parametrize("bad_port", [0, 70000])
def test_mcp_info_fails_loud_on_an_unadvertisable_port(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, bad_port: int
) -> None:
    """Why: port 0 binds an OS-chosen ephemeral port, so advertising `:0`
    hands out a URL that cannot reach the running gateway; out-of-range ports
    violate format: uri outright. Same fail-loud-and-name-the-setting contract
    as the host rows above."""
    _project_exists(monkeypatch)
    monkeypatch.setattr(
        "api.routers.health.get_settings",
        lambda: SimpleNamespace(
            mcp_http_host="127.0.0.1", mcp_http_port=bad_port, mcp_public_host=None
        ),
    )
    r = client.get("/projects/p/mcp")
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "INTERNAL"
    assert "mcp_http_port" in err["message"]
