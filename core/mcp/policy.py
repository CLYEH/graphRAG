"""Project query-policy loading (§21, DR-002; C8).

A project's registry config (``projects.config`` — the ONE SoR since CFG1/DR-012;
the file loaders below remain for the CLI's explicit ``--config`` override) carries
its ``query_policy`` block. That block is
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

from core.metadata.schema import MetadataExposure, load_metadata_exposure
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


def top_k_clamp_warning(policy: QueryPolicy, requested: int | None) -> dict[str, str] | None:
    """The §16 TRUNCATED warning owed when :meth:`QueryPolicy.top_k` clamped
    an over-cap ask — ``None`` when nothing was clamped.

    MCP13 (a): ``top_k()`` reconciles an over-cap ask via ``min()`` SILENTLY.
    A caller asking 9999 and receiving ``max_top_k`` results with empty
    warnings cannot distinguish "the corpus only has this many" from "you were
    clamped" — exactly the judgment (rephrase? page with the list_* tools?)
    the warning exists to inform; the OTHER end of the same parameter (a
    negative top_k) already refuses loudly. DESIGN §27.2 states the rule
    generally: any clamp must say so, silent clamping is forbidden.

    It lives HERE, beside the method that does the clamping, because BOTH
    query surfaces reconcile through that method and so both owe the same
    disclosure (QA4/#138: the MCP tools emitted this warning while the REST
    facade clamped silently, so one product answered the SAME request with two
    different stories about the completeness of its results). One message, one
    predicate, both surfaces — a second copy would be free to drift.

    Emitted at the FACADE layer, not inside the modes: the clamp happens when
    the request meets the policy, before any mode runs. A mode's own ceiling
    (sql's row cap, global's result cap) is a different truncation that the
    mode itself already reports.
    """
    if requested is None or requested <= policy.max_top_k:
        return None
    return {
        "code": "TRUNCATED",
        "message": (
            f"top_k={requested} exceeds the policy ceiling {policy.max_top_k} — "
            f"clamped to {policy.max_top_k} (§21 max_top_k); use the list_* tools "
            "to page beyond the retrieval ceiling"
        ),
    }


def load_query_policy(config_path: Path, *, text: str | None = None) -> QueryPolicy:
    """Load + validate ``query_policy`` from a project's ``config.yaml``.

    The file loader owns only the file/YAML/presence concerns; validation and
    typing are :func:`query_policy_from_mapping`'s — the same validator every
    registry consumer runs. Since CFG1/DR-012 the registry
    (``projects.config``) is the ONE policy SoR; this loader remains solely
    for the CLI's explicit ``--config`` override escape hatch. Every failure
    is a :class:`PolicyError` naming what broke.

    ``text`` supplies the file's ALREADY-READ content (``config_path`` is used only
    for error messages then): the eval worker reads golden + policy ONCE for its drift
    fingerprint and parses THAT text here, so the check and the scored bytes can't
    diverge (a TOCTOU re-read). Omit it and the file is read from ``config_path``.
    """
    try:
        raw = yaml.safe_load(config_path.read_text("utf-8") if text is None else text)
    except FileNotFoundError as exc:
        raise PolicyError(f"project config not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"project config is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or "query_policy" not in raw:
        raise PolicyError(f"project config {config_path} has no query_policy block")
    return query_policy_from_mapping(raw["query_policy"])


def query_policy_from_mapping(document: Any) -> QueryPolicy:
    """Validate + type a ``query_policy`` block from any source.

    Schema validation runs against the FROZEN contract first (DR-002 — the
    schema file is read fresh so a bumped contract is picked up, never
    vendored); the typed models then re-check the §21 frozen guarantees.
    """
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


async def _registry_config(conn: Any, project: str) -> dict[str, Any]:
    """The project's registry config mapping, policy-block-checked — the ONE
    row lookup both registry loaders share (a second copy would drift).

    The import is deferred: ``core.registry`` pulls store modules this
    policy-vocabulary module must not depend on at import time.
    """
    from core.registry import get_project

    row = await get_project(conn, project)
    if row is None:
        raise PolicyError(f"project {project!r} is not in the registry")
    config = row.config if isinstance(row.config, dict) else {}
    if "query_policy" not in config:
        raise PolicyError(
            f"project {project!r} has no query_policy block in its registry config "
            "(PATCH /projects/{project} writes it; CFG1: the registry is the ONLY source)"
        )
    return config


async def load_query_policy_from_registry(conn: Any, project: str) -> QueryPolicy:
    """Registry-sourced policy ONLY (CFG1) — for consumers that never touch
    metadata exposure (the CLI ``eval``): a malformed ``metadata_exposure``
    block must not block scoring a valid golden set + policy (Codex #93 R2:
    validating what the consumer does not use turns an unrelated config error
    into a false refusal)."""
    config = await _registry_config(conn, project)
    return query_policy_from_mapping(config["query_policy"])


async def load_runtime_config_from_registry(
    conn: Any, project: str
) -> tuple[QueryPolicy, MetadataExposure]:
    """Registry-sourced policy + exposure — the ONE SoR (CFG1).

    Owner 2026-07-17 superseded the 2026-07-10 dual-source decision: the
    Console API always read ``projects.config`` while MCP/CLI read
    ``projects/<name>/config.yaml``, letting the same project diverge between
    its human and agent surfaces. Every runtime consumer now reads the SAME
    registry column through the SAME shared validators
    (:func:`query_policy_from_mapping` / ``load_metadata_exposure``), so
    divergence is structurally impossible. Failures stay typed
    (:class:`PolicyError`) and fail loud — a project without a registry
    ``query_policy`` block cannot serve queries, same rule as the file era.
    """
    config = await _registry_config(conn, project)
    return query_policy_from_mapping(config["query_policy"]), load_metadata_exposure(config)
