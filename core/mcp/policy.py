"""Project query-policy loading (§21, DR-002; C8).

A project's ``config.yaml`` carries its ``query_policy`` block. That block is
validated against the FROZEN ``contracts/query_policy.schema.json`` before any
value is trusted — the schema is the contract, this module only carries it to
runtime (an invalid policy fails LOUD at server startup, never mid-query).
The typed models (:class:`~core.query.policy.TextToSql` /
:class:`~core.query.policy.TextToCypher`) re-check the frozen §21 guarantees
at construction, so a policy that somehow slipped the schema still cannot
under-guard.

Reconciliation lives here too (the C6b caller-reconciliation contract): the
mode functions take ALREADY-reconciled ceilings, and this is the caller —
``sql_rows()`` is ``min(max_sql_rows, text_to_sql.max_rows)``; ``top_k()``
clamps a request's ask to ``max_top_k``.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from core.query.policy import TextToCypher, TextToSql

#: Where the frozen schema can live: a source checkout keeps contracts/ at
#: the repo root; an installed wheel ships a build-time copy inside the core
#: package (pyproject force-include) — same bytes, same release, DR-002 holds
#: either way. Resolved lazily so a missing file names every candidate.
_SCHEMA_CANDIDATES = (
    Path(__file__).resolve().parent.parent.parent / "contracts" / "query_policy.schema.json",
    Path(__file__).resolve().parent.parent / "contracts" / "query_policy.schema.json",
)


def _schema_text() -> str:
    for candidate in _SCHEMA_CANDIDATES:
        if candidate.is_file():
            return candidate.read_text("utf-8")
    raise PolicyError(
        "query_policy.schema.json not found — looked in: "
        + ", ".join(str(c) for c in _SCHEMA_CANDIDATES)
    )


class PolicyError(ValueError):
    """The project's query policy is missing or violates the frozen contract.

    Raised at SERVER STARTUP (fail loud, §22's counterpart for config: a
    misconfigured guardrail must never run half-armed)."""


@dataclass(frozen=True)
class QueryPolicy:
    """The validated, typed view of one project's ``query_policy`` block."""

    default_mode: str
    max_top_k: int
    max_graph_hops: int
    max_sql_rows: int
    max_latency_ms: int
    expose_debug: bool
    text_to_sql: TextToSql
    text_to_cypher: TextToCypher

    def top_k(self, requested: int | None) -> int:
        """The effective result ceiling for one request: the caller's ask
        clamped to the policy cap; no ask → the cap itself. Out-of-contract
        asks are the TOOL's job to reject typed (§22) — this only reconciles
        values that already passed that gate."""
        if requested is None:
            return self.max_top_k
        return min(requested, self.max_top_k)

    def sql_rows(self) -> int:
        """§21: the sql row ceiling is the min of the top-level and the
        mode-local cap — the two can never disagree in the executor because
        only this reconciled value ever reaches it (C6b)."""
        return min(self.max_sql_rows, self.text_to_sql.max_rows)

    def sql_policy(self) -> TextToSql:
        """``text_to_sql`` with its per-phase deadline clamped to the
        top-level ``max_latency_ms`` (§21: the query deadline GOVERNS — a
        mode-local timeout above it would let one DB phase alone outlive the
        whole query's budget; C8 is the caller that loads both, so C8
        reconciles, the same min() contract as the row caps)."""
        return dataclasses.replace(
            self.text_to_sql,
            timeout_ms=min(self.text_to_sql.timeout_ms, self.max_latency_ms),
        )

    def cypher_policy(self) -> TextToCypher:
        """``text_to_cypher`` with its deadline clamped to ``max_latency_ms``
        — same reconciliation as :meth:`sql_policy`."""
        return dataclasses.replace(
            self.text_to_cypher,
            timeout_ms=min(self.text_to_cypher.timeout_ms, self.max_latency_ms),
        )


def load_query_policy(config_path: Path) -> QueryPolicy:
    """Load + validate ``query_policy`` from a project's ``config.yaml``.

    Schema validation runs against the FROZEN contract first (DR-002 — the
    schema file is read fresh so a bumped contract is picked up, never
    vendored); the typed models then re-check the §21 frozen guarantees.
    Every failure is a :class:`PolicyError` naming what broke.
    """
    try:
        raw = yaml.safe_load(config_path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"project config not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"project config is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or "query_policy" not in raw:
        raise PolicyError(f"project config {config_path} has no query_policy block")
    document = raw["query_policy"]

    schema = json.loads(_schema_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(part) for part in first.absolute_path) or "<root>"
        raise PolicyError(
            f"query_policy violates the frozen contract at {where}: {first.message}"
            + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else "")
        )

    try:
        text_to_sql = TextToSql.from_mapping(document["text_to_sql"])
        text_to_cypher = TextToCypher.from_mapping(document["text_to_cypher"])
    except ValueError as exc:
        raise PolicyError(f"query_policy failed the §21 frozen re-check: {exc}") from exc

    return QueryPolicy(
        default_mode=str(document["default_mode"]),
        max_top_k=int(document["max_top_k"]),
        max_graph_hops=int(document["max_graph_hops"]),
        max_sql_rows=int(document["max_sql_rows"]),
        max_latency_ms=int(document["max_latency_ms"]),
        expose_debug=bool(document["expose_debug"]),
        text_to_sql=text_to_sql,
        text_to_cypher=text_to_cypher,
    )


def hybrid_policy(
    policy: QueryPolicy,
    requested_top_k: int | None,
    latency_budget_ms: int | None = None,
) -> Any:
    """The :class:`~core.query.hybrid.HybridPolicy` slice for one request.

    ``latency_budget_ms`` is what the CALLER's clock has left of the §21
    budget (e.g. after scope binding) — hybrid's internal pacer starts from
    it, never from a fresh full ``max_latency_ms``, so the whole request
    respects the cap (clamped to the cap either way). None means the full
    budget (no outer clock).

    Imported lazily to keep this module free of the heavy query stack for
    callers that only need validation (e.g. a config linter)."""
    from core.query.hybrid import HybridPolicy

    budget = policy.max_latency_ms if latency_budget_ms is None else latency_budget_ms
    return HybridPolicy(
        text_to_sql=policy.sql_policy(),
        text_to_cypher=policy.cypher_policy(),
        max_graph_hops=policy.max_graph_hops,
        top_k=policy.top_k(requested_top_k),
        max_sql_rows=policy.sql_rows(),
        expose_debug=policy.expose_debug,
        max_latency_ms=min(budget, policy.max_latency_ms),
    )
