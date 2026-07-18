"""Why: §27's stored request identity is the replay/conflict ORACLE. When an
endpoint's ``produce`` re-reads its inputs under a row lock (eval — Codex #93
R7), the record must keep the hash of what was ACCEPTED, not the pre-lock read
a racing config write could skew — otherwise a retry under the accepted inputs
409s, and a config flipped back silently replays work scored against different
inputs. These pin ``run_idempotent``'s rekey mechanics at the statement layer.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.idempotency import run_idempotent

pytestmark = pytest.mark.contract


class _Result:
    def scalar_one_or_none(self) -> str:
        return "k"  # the reservation INSERT wins


class _Conn:
    def __init__(self) -> None:
        self.stmts: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.stmts.append(stmt)
        return _Result()


def _final_update_params(conn: _Conn) -> dict[str, Any]:
    compiled = conn.stmts[-1].compile()
    return dict(compiled.params)


async def _produce() -> tuple[int, dict[str, Any]]:
    return 202, {"ok": True}


async def test_a_winning_produce_stores_the_rekeyed_identity() -> None:
    conn = _Conn()
    status, _body = await run_idempotent(
        conn,  # type: ignore[arg-type]  # statement-capturing fake
        key="k",
        project="p",
        endpoint="e",
        req_hash="PRE-LOCK",
        produce=_produce,
        rekey=lambda: "ACCEPTED",
    )
    assert status == 202
    # the record's kept identity is what produce accepted under its lock —
    # replay/conflict decisions later match the pinned work, not the raced read
    assert _final_update_params(conn)["request_hash"] == "ACCEPTED"


async def test_without_rekey_the_initial_hash_is_kept() -> None:
    conn = _Conn()
    await run_idempotent(
        conn,  # type: ignore[arg-type]  # statement-capturing fake
        key="k",
        project="p",
        endpoint="e",
        req_hash="PRE-LOCK",
        produce=_produce,
    )
    # endpoints that never re-read under a lock keep §27 exactly as before:
    # the final UPDATE touches response/status only, never the stored hash
    assert "request_hash" not in _final_update_params(conn)


async def test_a_none_rekey_keeps_the_initial_hash() -> None:
    conn = _Conn()
    await run_idempotent(
        conn,  # type: ignore[arg-type]  # statement-capturing fake
        key="k",
        project="p",
        endpoint="e",
        req_hash="PRE-LOCK",
        produce=_produce,
        rekey=lambda: None,
    )
    # a produce that raised before its locked read has nothing to re-key with;
    # None must mean "keep req_hash", never store a NULL identity
    assert "request_hash" not in _final_update_params(conn)
