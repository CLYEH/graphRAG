"""Per-project MCP server factory (§9, C8).

Builds one FastMCP server exposing the frozen §9 tool set over ONE project:
the five retrieval tools (``semantic_search`` · ``graph_query`` ·
``global_summary`` · ``sql_query`` · ``hybrid_query`` — the default entry)
return the frozen §16 contract as-is (``McpResponse.to_dict()``); the three
auxiliary tools (``get_entity`` · ``list_schema`` · ``explain_retrieval``)
are §9-named conveniences: the §16 contract's ``tool`` enum freezes exactly
the five retrieval tools, so ``get_entity``/``list_schema`` return plain
INTROSPECTION shapes (not §16 — claiming the contract would violate its own
enum), while ``explain_retrieval`` returns the hybrid §16 response verbatim
(tool ``hybrid_query``) plus the trace gating below.

Every call re-binds to the ACTIVE build (DR-001, via
:meth:`~core.mcp.context.ProjectContext.bound` — activation between calls is
picked up; no store client is ever touched directly, DR-006). The query
policy is loaded from the REGISTRY at each session's lifespan start and
contract-validated (fail loud — :class:`~core.mcp.policy.PolicyError`;
CFG1: ``projects.config`` is the one SoR); ceilings are caller-reconciled here
(the C6b contract) before any mode function sees them. Tool arguments arrive
from an UNTRUSTED agent: the transport layer type-checks them
(FastMCP/pydantic), and the mode functions re-validate at their own doors
(the C6c/C6d lesson) — belt and braces, typed degradation either way.

Transports (§9 🔧): stdio (default) and streamable HTTP (C8b — the external
no-code agent platform consumes MCP over HTTP), selected at RUN time via
:func:`run_server`; the tools and policy are transport-agnostic, exactly the
additivity the original stdio-only note promised. HTTP binds
``core.config``'s ``mcp_http_host``/``mcp_http_port`` (localhost by default —
wider exposure is an operator opt-in while §23 auth remains a placeholder).
Entry point: ``graphrag serve-mcp`` (CFG1 gateway — one process, every
project at ``/mcp/<project>``); :func:`build_server` also serves stdio
one-project runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Annotated, Any, Final, cast

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from pydantic import Field, ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from core.llm.errors import LLM_CLIENT_ERRORS
from core.llm.factory import chat_model, query_embedding_model
from core.mcp.context import ProjectContext
from core.mcp.policy import (
    QueryPolicy,
    hybrid_policy,
    load_runtime_config_from_registry,
    top_k_clamp_warning,
)
from core.metadata.schema import MetadataExposure, unstorable_string_reason
from core.query.global_reports import global_summary as run_global
from core.query.graph import GraphQueryParams
from core.query.graph import graph_query as run_graph
from core.query.hybrid import hybrid_query as run_hybrid
from core.query.mentions import resolved_mention_refs
from core.query.metadata_enrich import enrich_response_metadata
from core.query.results import McpResponse, QueryWarning
from core.query.semantic import semantic_search as run_semantic
from core.query.sql import sql_query as run_sql
from core.stores import tables
from core.stores.errors import STORE_CLIENT_ERRORS, store_name
from core.stores.graph import graph_driver
from core.stores.repo import NoActiveBuildError
from core.stores.vectors import vector_client

#: §16 build_id is format:uuid — when the deadline fires DURING scope
#: binding no build was ever resolved; the nil uuid is the honest,
#: format-legal sentinel (the warning message says which case happened).
_NIL_BUILD = "00000000-0000-0000-0000-000000000000"

#: MCP12: hard cap on the query string every retrieval tool accepts. The
#: query is embedded / paraphrased through the model provider on the hot
#: path — without a cap an arbitrarily long input rides straight into
#: provider token limits and surfaces as a provider error (or cost). A §21
#: guardrail refusal at the door is actionable; a relayed provider error is
#: not. Generous for real questions (browse q caps are 64; this is 4000).
_QUERY_CHARS_CAP = 4000

#: MCP12: the DR-001 refusal every binding surface emits when the project
#: has no active build — REST maps the same condition to a 409; the MCP
#: envelope says it with a typed warning (NO_ACTIVE_BUILD, contract v1.2)
#: instead of letting the LookupError escape as a raw isError string.
_NO_ACTIVE_BUILD_MESSAGE = (
    "project has no active build — queries bind to builds.status='active' "
    "(DR-001); run a build and activate it, then retry"
)

#: MCP14 — server self-description. serverInfo.version previously reported
#: the MCP SDK's version (actively misleading: it identifies neither this
#: server nor the corpus); it now reports the graphrag package version. The
#: corpus identity is per-response (§16 build_id), not server metadata.
try:
    _SERVER_VERSION = importlib_metadata.version("graphrag")
except importlib_metadata.PackageNotFoundError:  # editable/dev checkout without dist metadata
    _SERVER_VERSION = "0.0.0-dev"

_REPO_URL = "https://github.com/CLYEH/graphRAG"

#: the frozen §16 response contract — advertised as the retrieval tools'
#: outputSchema (MCP14: it existed for every response yet tools/list showed
#: only additionalProperties:true). Two candidate locations, the
#: query-policy loader's rule (Codex #134 r1): a source checkout keeps
#: contracts/ at the repo root; an installed wheel ships a build-time copy
#: inside the core package (pyproject force-include).
_MCP_SCHEMA_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "contracts" / "mcp_response.schema.json",
    Path(__file__).resolve().parents[1] / "contracts" / "mcp_response.schema.json",
)


def _mcp_response_schema() -> dict[str, Any]:
    for candidate in _MCP_SCHEMA_CANDIDATES:
        if candidate.is_file():
            schema = cast(dict[str, Any], json.loads(candidate.read_text("utf-8")))
            return cast(dict[str, Any], _strip_schema_descriptions(schema))
    raise FileNotFoundError(
        "mcp_response.schema.json not found — looked in: "
        + ", ".join(str(c) for c in _MCP_SCHEMA_CANDIDATES)
    )


def _strip_schema_descriptions(node: Any, *, in_name_map: bool = False) -> Any:
    """The ADVERTISED copy of the contract drops every ``description``
    annotation (Codex #134 r2): the frozen file's descriptions carry DESIGN
    §-numbers, DR-ids and change-history notes an external agent cannot
    resolve — exactly the jargon MCP14 scrubbed from tool/param
    descriptions. Validation keywords are untouched (descriptions are pure
    annotations), and keys inside name→schema maps (``properties`` etc.)
    are data, never annotations — a real property NAMED "description" would
    survive."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if not in_name_map and key == "description" and isinstance(value, str):
                continue
            out[key] = _strip_schema_descriptions(
                value, in_name_map=key in ("properties", "patternProperties", "$defs")
            )
        return out
    if isinstance(node, list):
        return [_strip_schema_descriptions(item) for item in node]
    return node


#: what an external agent needs to know BEFORE the first call — it cannot
#: read docs/DESIGN.md, so the instructions carry the operating rules in
#: plain language (MCP14).
_SERVER_INSTRUCTIONS = """\
This server answers questions over ONE project's knowledge base (documents,
entities, relations, community reports) built by graphRAG.

Tool map:
- hybrid_query is the default entry: it fans out semantic + graph + sql
  retrieval and fuses the results. semantic_search / graph_query / sql_query /
  global_summary run a single mode. explain_retrieval is hybrid_query plus a
  routing trace (only when the operator enabled expose_debug).
- get_entity / get_chunk / get_document exchange ids from citations for full
  content. list_entities / list_chunks / list_reports browse the corpus with
  cursors — use them to see everything; retrieval responses are capped.
- list_schema shows the sql-queryable tables AND this session's policy
  ceilings (max_top_k, max_graph_hops, max_latency_ms, query/browse caps,
  enabled modes) — check it before probing limits by trial and error.

Reading responses:
- Retrieval tools return one envelope: results[] (each with source_refs
  citations), warnings[], and the build_id the answer was read from. Every
  call binds to the project's single ACTIVE build.
- warnings[] is how degradation is reported (the call itself succeeds):
  GUARDRAIL_BLOCKED = the call was refused, nothing was produced;
  TRUNCATED = results were clipped to a policy ceiling; MODE_SKIPPED /
  STORE_UNAVAILABLE / PARTIAL_RESULTS = a mode or store dropped out;
  NO_ACTIVE_BUILD = the project has no active build yet.
- Introspection tools (get_* / list_*) instead carry error + error_code:
  INVALID_INPUT (fix your input), NOT_FOUND (the id is not in the active
  build), QUERY_TIMEOUT (retry later), STORE_UNAVAILABLE (back off),
  NO_ACTIVE_BUILD (build and activate first). error_code null = success.
- Scores rank results within a response; they do not measure whether the
  corpus can answer the question — judge that from the returned content.
"""


#: the tools whose advertised outputSchema IS the frozen §16 contract, mapped
#: to the value each reports in the envelope's ``tool`` field. Those differ for
#: ``explain_retrieval``: it RUNS the hybrid router, and the frozen contract's
#: ``tool`` enum lists only the five retrieval modes — so its envelopes say
#: "hybrid_query", as its own success path already does. One mapping, two
#: consumers (the advertisement in _finalize_server_metadata and the refusal
#: shape in _argument_refusal): a tool added to one but not the other would
#: advertise the contract and then answer something else.
_ENVELOPE_TOOLS = {
    "semantic_search": "semantic_search",
    "graph_query": "graph_query",
    "global_summary": "global_summary",
    "sql_query": "sql_query",
    "hybrid_query": "hybrid_query",
    "explain_retrieval": "hybrid_query",
}

_ARGUMENT_HELP = (
    "Check tools/list for this tool's parameter types; every argument is "
    "validated before the query runs, so nothing was read and retrying the "
    "SAME call will fail identically."
)


def _readable_validation_error(exc: ValidationError) -> str:
    """A caller-actionable rendering of a pydantic failure.

    The raw ``str(exc)`` names the SDK's generated model
    (``semantic_searchArguments``) and links the pinned pydantic version's
    error docs — internals an unauthenticated caller has no use for and no
    business seeing, in place of the parameter-level fact it needs (QA5 D10).
    Only the field path and the human message survive."""
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
        parts.append(f"{where}: {err.get('msg', 'invalid value')}")
    return "; ".join(parts) or "the arguments did not validate"


def _bool_for_integer_reason(server: FastMCP, name: str, arguments: dict[str, Any]) -> str | None:
    """QA5 D12: pydantic coerces JSON ``true``/``false`` into 1/0 before any
    tool body runs, so a server-side ``type(limit) is not int`` guard is dead
    code and ``{"limit": true}`` becomes a perfectly successful 1-item page —
    a client bug that gets a green light (and 1405 round-trips instead of 8).
    ``false`` was worse: it reached the range check as 0, which then reported
    "got 0" for a value the caller never sent. The declared schema already
    says integer, so a bool is a caller error the surface should NAME."""
    tool = server._tool_manager._tools.get(name)  # noqa: SLF001 — no public accessor
    schema = getattr(tool, "parameters", None) if tool is not None else None
    props = (schema or {}).get("properties") or {}
    for key, value in arguments.items():
        if not isinstance(value, bool):
            continue
        declared = props.get(key) or {}
        types = {declared.get("type")} | {
            branch.get("type") for branch in declared.get("anyOf", []) if isinstance(branch, dict)
        }
        if "integer" in types:
            return f"{key}: expected an integer, got the boolean {str(value).lower()}"
    return None


def _argument_refusal(
    tool: str, detail: str, *, project: str, arguments: dict[str, Any]
) -> mcp_types.CallToolResult:
    """The typed shape for an argument rejected by the FRAMEWORK rather than
    by a tool body, IN THE SHAPE THAT TOOL ADVERTISES.

    Returned as a ``CallToolResult`` so the SDK hands it back verbatim rather
    than re-reporting it as an "Output validation error" that would bury the
    caller's real mistake. But bypassing OUR validator is not the same as
    being valid (Codex #140): the envelope tools advertise the frozen §16
    contract as their outputSchema, so a schema-driven client deserializes
    ``structuredContent`` against it and a bare ``{error, error_code}`` fails
    on ITS side — the refusal would be unreadable to exactly the careful
    clients that read the schema. Envelope tools therefore get a contract-
    valid §16 refusal (nil build, no results, GUARDRAIL_BLOCKED); the
    introspection tools keep their own error shape, which is what THEY
    advertise."""
    message = f"{tool}: {detail}. {_ARGUMENT_HELP}"
    if tool in _ENVELOPE_TOOLS:
        # the query may itself be the invalid argument (query=null is the
        # canonical case), so echo it only when it is usable as one
        raw = arguments.get("query")
        structured: dict[str, Any] = McpResponse(
            query=_safe_echo(raw) if isinstance(raw, str) else "",
            tool=_ENVELOPE_TOOLS[tool],
            project=project,
            build_id=_NIL_BUILD,
            results=(),
            warnings=(QueryWarning("GUARDRAIL_BLOCKED", message),),
        ).to_dict()
    else:
        structured = {"error": message, "error_code": "INVALID_INPUT"}
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=message)],
        structuredContent=structured,
        isError=True,
    )


def _guard_tool_dispatch(server: FastMCP, project: str) -> None:
    """Wrap the ONE seam every tool call passes through so framework-level
    argument failures answer in the same typed vocabulary the tools promise
    (QA5 D10/D12).

    ``FastMCP.call_tool`` is registered as the low-level handler at
    construction, so the wrapper is installed by RE-registering — the same
    ``request_handlers`` surgery :func:`_finalize_server_metadata` already
    performs. Wrapping here (rather than inside each tool) is what makes the
    guarantee exhaustive: validation happens BEFORE any tool body runs, so no
    tool can be added that forgets it. The raw ``arguments`` mapping is also
    only available here — before pydantic coerces it — which is the only
    place the bool-for-integer substitution is still visible.

    Classification is by PHASE, never by exception type (Codex #140 r2): the
    arguments are validated HERE, before ``inner`` runs, so a ValidationError
    caught in this scope is definitionally about the arguments. Reading it off
    ``ToolError.__cause__`` instead looked equivalent — the SDK does chain it
    — but the SDK wraps EVERY tool failure that way, so a pydantic error
    raised by a tool body or by a dependency decoding malformed store data
    would have been relabelled ``INVALID_INPUT`` and told the caller to fix a
    request that was fine. That is the same misdiagnosis class this task
    exists to end, and validating first makes it unreachable rather than
    merely unlikely. The double validation is a pure function of the
    arguments, so running it twice costs a little and risks nothing."""
    inner = server.call_tool

    async def guarded(name: str, arguments: dict[str, Any]) -> Any:
        reason = _bool_for_integer_reason(server, name, arguments)
        if reason is not None:
            return _argument_refusal(name, reason, project=project, arguments=arguments)
        tool = server._tool_manager._tools.get(name)  # noqa: SLF001 — no public accessor
        meta = getattr(tool, "fn_metadata", None)
        arg_model = getattr(meta, "arg_model", None)
        if arg_model is not None:
            try:
                # the SDK validates pre_parse_json(arguments), not the raw
                # mapping — clients JSON-STRINGIFY argument values (the SDK's
                # own note says Claude desktop "seems incapable of NOT doing
                # this"), so the two disagree in BOTH directions on the raw
                # dict: `{"q": "[1]"}` looks like a valid str to us while the
                # SDK parses it to a list and rejects (the D10 leak reopens
                # through its generic error), and `{"top_k": "null"}` parses
                # to a legal None for the SDK while we refuse a call it would
                # have served. Running the framework's OWN transform on the
                # framework's OWN input makes our verdict equal to its verdict
                # by construction rather than by resemblance.
                pre_parse = getattr(meta, "pre_parse_json", None)
                arg_model.model_validate(pre_parse(arguments) if callable(pre_parse) else arguments)
            except ValidationError as exc:
                return _argument_refusal(
                    name,
                    _readable_validation_error(exc),
                    project=project,
                    arguments=arguments,
                )
        # everything past here is the TOOL's own outcome — a fault inside it
        # flows to the SDK's error result untouched, still labelled as what it
        # is rather than as the caller's mistake
        return await inner(name, arguments)

    server._mcp_server.call_tool(validate_input=False)(guarded)  # noqa: SLF001


def _finalize_server_metadata(server: FastMCP) -> None:
    """MCP14 — metadata only, no execution-path change.

    (1) serverInfo.version → the graphrag package version (the SDK's own
    version was actively misleading). (2) prompts/resources handlers are
    UNREGISTERED: FastMCP declares both capabilities unconditionally while
    this server registers none — an agent following the declaration wasted
    two round-trips on empty lists; with the handlers gone the capabilities
    are no longer advertised (a client calling anyway gets the protocol's
    method-not-found, which is the truth). (3) the six §16 tools advertise
    the frozen response contract as their outputSchema — the cached_property
    slot is pre-filled so ONLY tools/list changes; runtime result conversion
    still uses fn_metadata (unchanged, proven by the call tests)."""
    server._mcp_server.version = _SERVER_VERSION  # noqa: SLF001 — the SDK exposes no public setter
    for request in (
        mcp_types.ListPromptsRequest,
        mcp_types.GetPromptRequest,
        mcp_types.ListResourcesRequest,
        mcp_types.ReadResourceRequest,
        mcp_types.ListResourceTemplatesRequest,
    ):
        server._mcp_server.request_handlers.pop(request, None)  # noqa: SLF001
    frozen = _mcp_response_schema()
    for name in _ENVELOPE_TOOLS:
        tool = server._tool_manager._tools[name]  # noqa: SLF001
        tool.__dict__["output_schema"] = frozen


#: the store CLIENTS' exception families (§22 STORE_UNAVAILABLE) and their
#: store names now live in core.stores.errors — hybrid's per-mode guard uses
#: the same map, so the two degradation surfaces cannot drift (Codex #122 r3).
#: Deliberately NOT Exception either way: an in-code bug still propagates
#: LOUD — degradation is for store trouble, never for our own bugs.
_STORE_ERRORS: tuple[type[BaseException], ...] = STORE_CLIENT_ERRORS
_store_name = store_name


def _echoable(query: str) -> str:
    """The §16 envelope echo of a caller's query: a WITHIN-cap query is
    returned whole (clients correlate, log, and retry from the envelope —
    Codex #133 r2), while a genuinely oversized one is truncated to 200
    chars so no refusal path can ever amplify a large input (Codex #133 r1)
    — the backstop for the tools' cap-first ordering."""
    return query if len(query) <= _QUERY_CHARS_CAP else query[:200]


def _safe_echo(value: str, limit: int = 200) -> str:
    """A version of ``value`` that can survive the response's own JSON
    encoding. An unpaired UTF-16 surrogate is legal in a JSON *document* but
    cannot be encoded to UTF-8, so echoing a caller's malformed string whole
    turns a REFUSAL into a raw ``UnicodeEncodeError`` from the transport (QA5,
    D11) — the caller then learns nothing about what it sent wrong. Each bad
    code unit becomes U+FFFD so the echo still shows the SHAPE of the input.

    The echo is a best-effort AID, never a claim: it is also windowed to
    ``limit``, so the offending unit may not appear in it at all. No refusal
    message describes the echo's contents for exactly that reason — what the
    caller must act on is the code point, which the reason names outright
    (see :func:`_unusable_text_reason`).

    The slice comes BEFORE the substitution (Codex #140): transforming the
    whole value first and slicing after would let a multi-MB malformed
    argument drive an O(len) transient allocation that the advertised
    80/200-char window does not bound — the caller is refused either way, so
    only the echoed prefix is worth touching. Slicing is 1:1 with the
    substitution (each code unit maps to one), so the result is identical."""
    return "".join(
        "�" if ch == "\x00" or 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in value[:limit]
    )


def _unusable_text_reason(value: str, *, allow_blank: bool = False) -> str | None:
    """The ONE predicate behind every QA5 refusal — why ``value`` cannot be
    served as given, or None when it can.

    Storability defers to ``unstorable_string_reason``, the guard the WRITE
    path already uses (MCP10), so both directions agree on exactly what
    Postgres can hold instead of drifting apart. ``allow_blank`` marks a field
    where emptiness is MEANINGFUL rather than a mistake — a
    ``list_entities(q=...)`` filter browses everything, unlike a question or
    an exact-name lookup, where blank can only be a bug.

    The reason names the offending CODE POINT ("contains a NUL character
    (U+0000)", "contains an unpaired UTF-16 surrogate") and deliberately says
    nothing about the echo that accompanies it. Earlier revisions appended a
    "U+FFFD marks it in the echoed value" legend and were wrong four times
    over — the substitution character was actually ``?``; then the value was
    not echoed at all; then a blank value carried the legend; then a long
    blank re-earned it through truncation; then a bad unit past the 80-char echo
    window fell outside it. Each fix moved the flaw rather than ending it,
    because the CLAIM and the ARTIFACT it described were produced by
    different code under different rules (this predicate knows the
    substitution set, the payload builder owns the echo window). A message
    that asserts nothing about the echo cannot be wrong about it, and the
    code point already tells the caller exactly what to remove."""
    reason = unstorable_string_reason(value)
    if reason is not None:
        return f"contains a character this store cannot hold — {reason}"
    if not allow_blank and not value.strip():
        return "is empty or only whitespace"
    return None


def _unusable_param_payload(
    project: str,
    tool: str,
    label: str,
    field: str,
    value: str,
    *,
    allow_blank: bool = False,
) -> dict[str, Any] | None:
    """The §16-shaped pre-binding refusal for a malformed tool PARAMETER, or
    None when it is usable.

    Every free-text parameter that reaches a store needs this, not just the
    question: ``graph_query``'s ``entity``/``other_entity`` are resolved by a
    Postgres name lookup, and guarding only the echoed label left the seed
    itself unguarded — a caller supplying a clean ``query`` label with a
    malformed seed still reached the store and drew the fake
    ``STORE_UNAVAILABLE`` (the D5 mechanism through a different door)."""
    reason = _unusable_text_reason(value, allow_blank=allow_blank)
    if reason is None:
        return None
    # the offending VALUE is named, not just the field: a call can carry
    # several free-text parameters, and "one of them is malformed" is not
    # something a caller can act on. Whether the echo carries a U+FFFD is the
    # reason's business (see _unusable_text_reason) — deciding it here meant
    # comparing echo against value, which also fires on plain truncation.
    return McpResponse(
        query=_safe_echo(label),
        tool=tool,
        project=project,
        build_id=_NIL_BUILD,
        results=(),
        warnings=(
            QueryWarning(
                "GUARDRAIL_BLOCKED",
                f"{field} {reason} (received {_safe_echo(value, 80)!r}) — this "
                "is an INPUT problem, not a store outage; fix the parameter "
                "and retry",
            ),
        ),
    ).to_dict()


def _unusable_query_payload(project: str, tool: str, query: str) -> dict[str, Any] | None:
    """The shared pre-binding refusal for a query that cannot be served AS
    GIVEN — or None when it is usable.

    §21 length (MCP12; extracted for MCP13 — every path that would ECHO the
    query must run this FIRST, including tool-level early returns that bypass
    ``_bounded``, or an oversized input is reflected whole: response
    amplification and a cap the surface no longer shares — Codex #133 r1).
    The echo is truncated to 200 chars for the same reason.

    QA5 adds the two malformed classes this gate used to let through, because
    a refusal HERE is the only one that can name the real cause:
    - a NUL or unpaired surrogate reached Postgres and came back as a
      ``DBAPIError``, which the §22 handler faithfully reported as
      ``STORE_UNAVAILABLE`` "postgres unavailable" — one byte and the server
      LIED about infrastructure health, telling a contract-abiding agent to
      back off from a database that was fine (D5). The predicate is the SAME
      ``unstorable_string_reason`` the write path uses, so both directions
      agree on what Postgres cannot hold.
    - a whitespace-only query ran a real retrieval and returned scored
      results (the corpus's own "（未指明）" placeholder ranked first) with no
      warning at all — a confident answer to a question nobody asked (D9).
      An EMPTY query only failed once it reached the embedding provider,
      which blamed "the model provider" for the caller's own input.

    Length is checked FIRST (Codex #140 r5): it is an O(1) ``len`` while the
    storability/blankness scan is a Python-level per-character pass, and MCP
    arguments carry no schema-level max length, so scanning a multi-MB query
    before the cap rejects it would hand an attacker O(len) work for an input
    the cap refuses anyway. An oversized query is refused on length alone —
    its content is never scanned — and its echo runs through ``_safe_echo``
    like every other, so an oversized-AND-malformed query cannot smuggle a
    lone surrogate into the refusal and kill its own serialization.
    """
    if len(query) > _QUERY_CHARS_CAP:
        return McpResponse(
            query=_safe_echo(query, 200),
            tool=tool,
            project=project,
            build_id=_NIL_BUILD,
            results=(),
            warnings=(
                QueryWarning(
                    "GUARDRAIL_BLOCKED",
                    f"query length {len(query)} exceeds the {_QUERY_CHARS_CAP}-char cap "
                    "— shorten the query (§21); rejected, not clamped",
                ),
            ),
        ).to_dict()
    return _unusable_param_payload(project, tool, query, "query", query)


def _with_clamp_warning(
    payload: dict[str, Any], policy: QueryPolicy, requested: int | None
) -> dict[str, Any]:
    """Append the top_k clamp warning to a §16 payload when it applies.

    A nil-build payload is a pre-binding refusal (oversized query, no active
    build) or a binding stall — the retrieval never ran, so no clamp ever
    happened and claiming one would overstate the response."""
    if payload["build_id"] == _NIL_BUILD:
        return payload
    clamp = top_k_clamp_warning(policy, requested)
    if clamp is not None:
        payload["warnings"] = [*payload["warnings"], clamp]
    return payload


async def _bounded(
    runtime: _Runtime,
    tool: str,
    query: str,
    runner: Any,
) -> dict[str, Any]:
    """Run one single-mode tool under the project's §21 wall-clock deadline.

    The timeout covers the per-call binding TOO — connection acquisition or
    the active-build lookup can itself stall under DB/network pressure, and a
    §21 deadline that starts after binding would let the call overrun before
    the typed degradation (this is the same rule for every tool; hybrid keeps
    its own richer internal deadline for the mode loop). A timeout is the
    typed §22 degradation, never a hung call or an unhandled cancellation;
    the sql reader's phase transactions roll back under the single
    cancellation (finally runs), and the per-call connection closes with the
    context manager either way."""
    bound_build: str | None = None
    # length check needs no store — refuse an oversized query BEFORE the
    # binding opens one (MCP12: the query rides into the model provider's
    # token limits; a §21 refusal here is actionable, a provider error not)
    unusable = _unusable_query_payload(runtime.context.project, tool, query)
    if unusable is not None:
        return unusable
    deadline = time.monotonic() + runtime.policy.max_latency_ms / 1000.0
    try:
        async with asyncio.timeout(runtime.policy.max_latency_ms / 1000.0):
            async with runtime.context.bound() as deps:
                bound_build = str(deps.repo.build_id)
                # the runner gets what binding LEFT of the budget — a pacer
                # inside it (hybrid) starts from the REMAINDER, never a
                # fresh full budget, so the whole call respects the cap and
                # the inner deadline beats this outer one in all but a μs
                # photo finish (either way a typed §22 cut; partial assembly
                # stays with the pacer, and the outer cut covers what no
                # inner timer can see — the binding itself)
                remaining_ms = max(int((deadline - time.monotonic()) * 1000), 1)
                response: McpResponse = await runner(deps, remaining_ms)
                # enrich chunk source_refs with the exposed slice of their
                # document metadata (DR-010 rule 6/7) — one place for every
                # modality, inside the deadline + the build-scoped binding
                response = await enrich_response_metadata(response, deps.repo, runtime.exposure)
                return response.to_dict()
    except TimeoutError:
        detail = "" if bound_build else " during scope binding"
        return McpResponse(
            query=query,
            tool=tool,
            project=runtime.context.project,
            # binding may itself be what stalled — then no build was ever
            # resolved and the nil uuid marks that honestly (format-legal)
            build_id=bound_build or _NIL_BUILD,
            results=(),
            warnings=(
                QueryWarning(
                    "PARTIAL_RESULTS",
                    f"query exceeded the {runtime.policy.max_latency_ms}ms deadline{detail} (§21)",
                ),
            ),
        ).to_dict()
    except NoActiveBuildError:
        # DR-001 lifecycle state, not store trouble: REST answers 409, the
        # MCP envelope answers with the typed NO_ACTIVE_BUILD warning
        # (contract v1.2) — previously this LookupError escaped as a raw
        # isError string (MCP12)
        return McpResponse(
            query=query,
            tool=tool,
            project=runtime.context.project,
            build_id=_NIL_BUILD,
            results=(),
            warnings=(QueryWarning("NO_ACTIVE_BUILD", _NO_ACTIVE_BUILD_MESSAGE),),
        ).to_dict()
    except _STORE_ERRORS as exc:
        # a store outage during binding or the mode run degrades typed
        # (§22 STORE_UNAVAILABLE), never an MCP transport error; hybrid maps
        # per-mode internally — this is the single-mode tools' equivalent
        return McpResponse(
            query=query,
            tool=tool,
            project=runtime.context.project,
            build_id=bound_build or _NIL_BUILD,
            results=(),
            warnings=(
                QueryWarning(
                    "STORE_UNAVAILABLE",
                    f"{_store_name(exc)} unavailable ({type(exc).__name__}) — degraded "
                    "to an empty typed response (§22)",
                ),
            ),
        ).to_dict()
    except LLM_CLIENT_ERRORS as exc:
        # the model provider sits on the hot path (embedding / NL→SQL /
        # hybrid routing) — a single 429 must degrade TYPED, never a raw
        # isError; the message names only the exception CLASS (upstream
        # error text — provider identity, model token ceilings — is never
        # relayed to an untrusted caller). Hybrid classifies per-mode with
        # the same input-vs-infrastructure rule (MCP2); this is the
        # single-mode equivalent (MCP12).
        status = getattr(exc, "status_code", None)
        if not isinstance(exc, STORE_CLIENT_ERRORS) and status in (400, 422):
            warning = QueryWarning(
                "GUARDRAIL_BLOCKED",
                f"the model provider rejected the request input "
                f"({type(exc).__name__}, HTTP {status}) — the request as issued "
                "cannot be served; retrying unchanged will fail again (§22)",
            )
        else:
            warning = QueryWarning(
                "STORE_UNAVAILABLE",
                f"model provider unavailable ({type(exc).__name__}) — degraded "
                "to an empty typed response; retry later (§22)",
            )
        return McpResponse(
            query=query,
            tool=tool,
            project=runtime.context.project,
            build_id=bound_build or _NIL_BUILD,
            results=(),
            warnings=(warning,),
        ).to_dict()


def _is_statement_timeout(exc: DBAPIError) -> bool:
    """Postgres cancels a statement past ``statement_timeout`` with sqlstate
    57014 (query_canceled) — the DB-side face of the §21 deadline."""
    return getattr(exc.orig, "sqlstate", None) == "57014"


def _introspection_store_error(
    runtime: _Runtime, build_id: str | None, subject: str, exc: BaseException
) -> dict[str, Any]:
    """The introspection tools' §22 store-outage shape — the same explicit
    error field as the timeout shape, naming the store exception class."""
    return {
        "project": runtime.context.project,
        "build_id": build_id or _NIL_BUILD,
        "subject": subject,
        "error": f"{_store_name(exc)} unavailable ({type(exc).__name__}) — §22",
        "error_code": "STORE_UNAVAILABLE",
    }


def _introspection_no_active_build(runtime: _Runtime, subject: str) -> dict[str, Any]:
    """The introspection tools' DR-001 refusal shape — the same typed
    ``error`` field as the timeout/store shapes (MCP12: the NoActiveBuildError
    LookupError previously escaped every introspection tool as a raw isError
    string). Nil build: no build was ever resolved, by definition."""
    return {
        "project": runtime.context.project,
        "build_id": _NIL_BUILD,
        "subject": subject,
        "error": _NO_ACTIVE_BUILD_MESSAGE,
        "error_code": "NO_ACTIVE_BUILD",
    }


def _introspection_timeout(runtime: _Runtime, build_id: str | None, subject: str) -> dict[str, Any]:
    """The introspection tools' §22 timeout shape (they are not §16 responses,
    so the degradation is an explicit error field, never a hung call). A None
    build_id means the deadline fired during scope binding — nil-uuid
    sentinel, same convention as the §16 tools."""
    detail = "" if build_id else " during scope binding"
    return {
        "project": runtime.context.project,
        "build_id": build_id or _NIL_BUILD,
        "subject": subject,
        "error": f"query exceeded the {runtime.policy.max_latency_ms}ms deadline{detail} (§21)",
        "error_code": "QUERY_TIMEOUT",
    }


@dataclass
class _Runtime:
    """The lifespan-held state: one context + one validated policy + the
    document-metadata exposure allowlist (fail-closed empty by default)."""

    context: ProjectContext
    policy: QueryPolicy
    exposure: MetadataExposure = field(default_factory=lambda: MetadataExposure(fields=()))


async def _load_runtime_config(engine: Any, project: str) -> tuple[QueryPolicy, MetadataExposure]:
    """One connection, one registry read (CFG1) — the lifespan's policy seam,
    module-level so hermetic tests can stub the WHOLE acquisition (fake
    engines carry no ``connect``)."""
    async with engine.connect() as conn:
        return await load_runtime_config_from_registry(conn, project)


def build_server(project: str) -> FastMCP:
    """One project's MCP server — policy read from the REGISTRY per session.

    CFG1: ``projects.config`` is the ONE policy SoR (owner 2026-07-17,
    superseding the 2026-07-10 dual-source decision) — no ``config.yaml``.
    The load moved from build time to LIFESPAN start, which the SDK enters
    once per protocol session: a policy edit applies to the NEXT session,
    and a project with a missing/invalid registry policy fails that
    session's startup loud (typed :class:`~core.mcp.policy.PolicyError`),
    never half-serves.

    MCP16 — store/model clients are built ONCE per server instance and
    SHARED across its sessions. Measured: the concurrency ramp (5→15→30→60
    sessions, initialize+get_entity, pure Postgres) degraded linearly
    5.3s→40.0s at a fixed ~0.8 session/s with PG connections peaking at 7 —
    serialization, not the database. Root cause: ``AsyncQdrantClient``'s
    constructor performs synchronous blocking I/O (measured 1,302ms
    event-loop stall) and ran once per session lifespan, freezing every
    other in-flight session on the single-threaded loop. Now the first
    session builds the bundle (the blocking constructors under
    ``asyncio.to_thread``, so even that one build never stalls the loop)
    and later sessions reuse it — which also stops the per-session
    engine+Qdrant+Neo4j+2×OpenAI accumulation. The POLICY stays per-session
    (DR-012), read before the bundle is touched (Codex #93 R5: a policy
    error must surface, not a masking client error). Sessions no longer
    close the clients; the OWNER of the server instance does — the gateway
    host task after the child app's lifespan exits (eviction/shutdown), or
    :func:`run_server` after a stdio run — via the ``close`` handle on the
    returned server (``_graphrag_close_shared``)."""

    shared: dict[str, Any] = {}
    shared_lock: dict[str, asyncio.Lock] = {}  # created lazily inside the running loop

    async def _shared_context() -> ProjectContext:
        # the lock itself is minted lazily (asyncio.Lock at import/build time
        # would bind no running loop); dict-setdefault is atomic enough here
        # because the FIRST await below is what yields control
        lock = shared_lock.setdefault("lock", asyncio.Lock())
        async with lock:
            if "context" not in shared:
                settings = get_settings()
                engine = create_async_engine(
                    settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
                    poolclass=NullPool,
                )
                # the measured loop-blockers (Qdrant client construction:
                # 1,302ms stall; the OpenAI model factories import/build
                # heavyweight clients) go to a worker thread — none of them
                # needs the running loop to construct
                qdrant, embedder, llm = await asyncio.to_thread(
                    lambda: (vector_client(), query_embedding_model(), chat_model())
                )
                shared["context"] = ProjectContext(
                    project=project,
                    engine=engine,
                    qdrant=qdrant,
                    neo4j=graph_driver(),
                    embedder=embedder,
                    llm=llm,
                )
        context: ProjectContext = shared["context"]
        return context

    async def _close_shared() -> None:
        context = shared.pop("context", None)
        if context is not None:
            await context.aclose()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[_Runtime]:
        settings = get_settings()
        # policy BEFORE any store/model client (Codex #93 R5): when the
        # registry policy is missing/invalid AND a client factory would also
        # fail (e.g. no OPENAI_API_KEY), startup must surface the actionable
        # typed PolicyError, not the masking client error. The policy read
        # uses its OWN throwaway engine so a failure here cannot poison or
        # tear down the shared bundle other live sessions are using.
        policy_engine = create_async_engine(
            settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool,
        )
        try:
            policy, exposure = await _load_runtime_config(policy_engine, project)
        finally:
            await policy_engine.dispose()
        context = await _shared_context()
        # NO per-session close: the bundle outlives the session (MCP16) —
        # the server owner closes it via _graphrag_close_shared
        yield _Runtime(context=context, policy=policy, exposure=exposure)

    # host/port are read at BUILD time like the policy (a later env change
    # applies on the next build); they only matter for the http transport —
    # stdio ignores them
    http_settings = get_settings()
    server = FastMCP(
        f"graphrag-{project}",
        lifespan=lifespan,
        host=http_settings.mcp_http_host,
        port=http_settings.mcp_http_port,
        # MCP14: the server describes ITSELF — an external agent cannot read
        # docs/DESIGN.md, so the operating rules ride the initialize response
        instructions=_SERVER_INSTRUCTIONS,
        website_url=_REPO_URL,
    )

    def _rt() -> _Runtime:
        # SESSION-scoped, via the SDK's own channel: Server.run enters the
        # lifespan once per protocol session and parks the yielded value on
        # that session's request context — and streamable HTTP multiplexes
        # MANY sessions on one process (Codex #58 P1). A module-level slot
        # here would be overwritten by every later session's startup and
        # would hand tools already-closed store clients once any session
        # ends; the request context always resolves to the CALLING session's
        # own runtime. (stdio = exactly one session; behavior unchanged.)
        rt: _Runtime = server.get_context().request_context.lifespan_context
        return rt

    @server.tool()
    async def semantic_search(
        query: Annotated[str, Field(description="The question or topic, natural language.")],
        top_k: Annotated[
            int | None,
            Field(
                description=(
                    "Max results; omitted = the policy ceiling. Over-cap asks are clamped WITH a "
                    "TRUNCATED warning."
                )
            ),
        ] = None,
        point_type: Annotated[
            str | None,
            Field(
                description=(
                    'Restrict results: "chunk" = text passages only, "entity" = name matches only; '
                    "omitted = both."
                ),
                json_schema_extra={"enum": ["chunk", "entity", None]},
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Fuzzy/topical retrieval over the document text (semantic mode).

        Results mix text chunks and entity name matches; each type is
        guaranteed up to half the page, and entity titles carry the ontology
        type — pass point_type="chunk" for passages only, or "entity" for
        name lookup only.

        Scores are cosine similarities: they RANK results within this
        response but do not measure whether the corpus can answer the
        question — an unanswerable question still returns its nearest
        chunks, and measured on real data no score threshold separates the
        two cases (topically-close-but-absent questions outscore answerable
        generic ones). No warning flags an out-of-domain question; judge
        answerability from the returned content, not from the scores:
        chunk results carry the matched text; entity results carry the
        matched NAME plus quoted mention citations — a citation with
        source_type "chunk" has a chunk UUID id (exchange it for full text
        with get_chunk), one with source_type "row" cites a structured
        table+pk instead (get_chunk does not accept those). A page of
        bare name matches is still NOT evidence the corpus answers the
        question."""
        rt = _rt()

        async def _run(deps: Any, _remaining_ms: int) -> McpResponse:
            return await run_semantic(
                deps.repo, deps.vectors, deps.embedder, query, rt.policy.top_k(top_k), point_type
            )

        payload = await _bounded(rt, "semantic_search", query, _run)
        return _with_clamp_warning(payload, rt.policy, top_k)

    @server.tool()
    async def graph_query(
        template: Annotated[
            str,
            Field(
                description=(
                    'Traversal shape: "neighbors" (what connects to '
                    'entity), "path" (route from entity '
                    'to other_entity), or "subgraph" (the region around entity).'
                ),
                json_schema_extra={"enum": ["neighbors", "path", "subgraph"]},
            ),
        ],
        entity: Annotated[
            str,
            Field(
                description="Seed entity, EXACT canonical name (find it with list_entities q=...)."
            ),
        ],
        other_entity: Annotated[
            str | None,
            Field(
                description=(
                    'Destination entity — required by the "path" template, meaningless elsewhere.'
                )
            ),
        ] = None,
        hops: Annotated[
            int,
            Field(
                description="Traversal depth; above the policy ceiling is rejected, not clamped."
            ),
        ] = 1,
        query: Annotated[
            str, Field(description="Optional label echoed in the response envelope.")
        ] = "",
    ) -> dict[str, Any]:
        """Entity-relationship retrieval via parameterized graph templates
        (neighbors / path / subgraph). Relation results cite evidence refs;
        a ref with source_type "chunk" carries a chunk UUID exchangeable for
        its text via get_chunk (row/document evidence refs are other shapes —
        get_chunk does not accept them)."""
        rt = _rt()
        # QA5 D5 (sibling of the query guard): the SEED names are free text
        # resolved by a Postgres name lookup, and _bounded only guards the
        # echoed LABEL — with a clean `query` supplied, a malformed seed
        # sailed past it into the store and drew the fake STORE_UNAVAILABLE
        label = query or f"{template}({entity})"
        for param, value in (("entity", entity), ("other_entity", other_entity)):
            if value is None:
                continue
            unusable = _unusable_param_payload(
                rt.context.project, "graph_query", label, param, value
            )
            if unusable is not None:
                return unusable
        params = GraphQueryParams(
            template=template, entity=entity, other_entity=other_entity, hops=hops
        )

        async def _run(deps: Any, _remaining_ms: int) -> McpResponse:
            return await run_graph(
                deps.graph,
                deps.repo,
                rt.policy.cypher_policy(),
                params,
                label,
                rt.policy.max_graph_hops,
            )

        return await _bounded(rt, "graph_query", label, _run)

    @server.tool()
    async def sql_query(
        query: Annotated[
            str,
            Field(
                description=(
                    "Natural-language question over the structured tables (see list_schema); "
                    "translated to guarded read-only SQL."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Precise filters/lookups over structured rows (guarded natural-language→SQL)."""
        rt = _rt()

        async def _run(deps: Any, _remaining_ms: int) -> McpResponse:
            return await run_sql(
                deps.sql_reader, deps.llm, rt.policy.sql_policy(), query, rt.policy.sql_rows()
            )

        return await _bounded(rt, "sql_query", query, _run)

    @server.tool()
    async def global_summary(
        query: Annotated[
            str,
            Field(
                description="Echoed in the envelope; results are rating-ranked, not query-matched."
            ),
        ],
        top_k: Annotated[
            int | None,
            Field(
                description=(
                    "Max reports; omitted = the policy ceiling. Over-cap asks are clamped WITH a "
                    "TRUNCATED warning."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Corpus-wide community summaries — a rating-ranked overview of the
        whole corpus (use list_reports to page through ALL of them)."""
        rt = _rt()

        async def _run(deps: Any, _remaining_ms: int) -> McpResponse:
            return await run_global(deps.repo, query, rt.policy.top_k(top_k))

        payload = await _bounded(rt, "global_summary", query, _run)
        return _with_clamp_warning(payload, rt.policy, top_k)

    @server.tool()
    async def hybrid_query(
        query: Annotated[str, Field(description="The question, natural language.")],
        top_k: Annotated[
            int | None,
            Field(
                description=(
                    "Max fused results; omitted = the policy "
                    "ceiling. Over-cap asks are clamped WITH a "
                    "TRUNCATED warning."
                )
            ),
        ] = None,
        graph_template: Annotated[
            str | None,
            Field(
                description=(
                    "Your explicit graph invocation's traversal shape "
                    "— must be supplied TOGETHER with "
                    "graph_entity."
                ),
                json_schema_extra={"enum": ["neighbors", "path", "subgraph", None]},
            ),
        ] = None,
        graph_entity: Annotated[
            str | None,
            Field(
                description=(
                    "Your explicit graph invocation's seed entity (exact canonical name) — must be "
                    "supplied TOGETHER with graph_template."
                )
            ),
        ] = None,
        graph_other_entity: Annotated[
            str | None,
            Field(description='Optional refinement: the "path" template\'s destination entity.'),
        ] = None,
        graph_hops: Annotated[
            int | None,
            Field(
                description=(
                    "Optional refinement: traversal depth (defaults "
                    "to 1 for a complete invocation)."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Fan every AVAILABLE mode out over the question and fuse: semantic
        + graph + sql, deterministically (no LLM routing). Community reports
        are NOT fused — they are rating-ranked corpus overview, never
        query-matched; use global_summary for that. Supply graph_template +
        graph_entity to run YOUR graph invocation; without them the router
        derives a safe plan itself when the question names a build entity
        (an automatic plan, visible in the routing trace). Supplying ANY graph_*
        parameter without BOTH graph_template and graph_entity is refused
        loudly (GUARDRAIL_BLOCKED, zero results) — the router never silently
        substitutes its own plan for half of yours. graph_hops defaults to 1
        when your invocation omits it.

        `score` is rank-fusion (RRF) ordering, not confidence; each result's
        `confidence` carries its origin mode's RAW score (cosine for
        semantic) — comparable within a mode, not across modes. No warning
        flags an out-of-domain question (see semantic_search on why scores
        cannot) — judge answerability from the returned content. Text
        passages are guaranteed up to half the fused page whenever the
        semantic mode returns any — a floor of top_k//2, so at top_k=1 there
        is no half to reserve and rank alone decides; for factual text
        questions semantic_search alone is often faster."""
        rt = _rt()
        # cap FIRST (the explain_retrieval ordering, Codex #133 r1 class):
        # the incomplete-invocation refusal below is a pre-_bounded early
        # return that echoes the query — unchecked, an oversized query with
        # a half graph invocation would be reflected whole
        unusable = _unusable_query_payload(rt.context.project, "hybrid_query", query)
        if unusable is not None:
            return unusable
        refused = _incomplete_graph_invocation_payload(
            rt.context.project,
            query,
            graph_template=graph_template,
            graph_entity=graph_entity,
            graph_other_entity=graph_other_entity,
            graph_hops=graph_hops,
        )
        if refused is not None:
            return refused
        # QA5 D5: same seed exposure as graph_query — the guard above covers
        # the QUERY, these reach the store as entity names
        for param, seed in (
            ("graph_entity", graph_entity),
            ("graph_other_entity", graph_other_entity),
        ):
            if seed is None:
                continue
            unusable = _unusable_param_payload(
                rt.context.project, "hybrid_query", query, param, seed
            )
            if unusable is not None:
                return unusable
        params: GraphQueryParams | None = None
        if graph_template is not None and graph_entity is not None:
            params = GraphQueryParams(
                template=graph_template,
                entity=graph_entity,
                other_entity=graph_other_entity,
                hops=graph_hops if graph_hops is not None else 1,
            )

        async def _run(deps: Any, remaining_ms: int) -> McpResponse:
            # hybrid's internal pacer runs on what binding LEFT of the §21
            # budget — never a fresh full one — so its earlier deadline wins
            # the terminal cut in all but a μs photo finish (partial
            # assembly) and the whole call stays within max_latency_ms
            return await run_hybrid(
                deps,
                hybrid_policy(rt.policy, top_k, latency_budget_ms=remaining_ms),
                query,
                params,
            )

        payload = await _bounded(rt, "hybrid_query", query, _run)
        return _with_clamp_warning(payload, rt.policy, top_k)

    @server.tool()
    async def get_entity(
        name: Annotated[
            str,
            Field(description="EXACT canonical entity name (find it with list_entities q=...)."),
        ],
    ) -> dict[str, Any]:
        """Look one entity up by EXACT canonical name — unsure of the name?
        Use list_entities(q=...) for substring search first. Introspection
        shape (error/error_code, not the retrieval envelope); each entity
        carries its full, uncapped mention citations."""
        rt = _rt()
        # QA5 D5: validate BEFORE binding — a NUL here used to reach Postgres
        # and come back as a DBAPIError the §22 handler reported as
        # "postgres unavailable", i.e. the server blaming a healthy store for
        # the caller's own byte
        unusable = _unusable_subject_payload(rt.context.project, "name", name)
        if unusable is not None:
            return unusable
        bound_build: str | None = None
        try:
            async with asyncio.timeout(rt.policy.max_latency_ms / 1000.0):
                async with rt.context.bound() as deps:
                    bound_build = str(deps.repo.build_id)
                    return await _get_entity(deps.repo, rt.context.project, name)
        except NoActiveBuildError:
            return _introspection_no_active_build(rt, name)
        except TimeoutError:
            return _introspection_timeout(rt, bound_build, name)
        except _STORE_ERRORS as exc:
            return _introspection_store_error(rt, bound_build, name, exc)

    @server.tool()
    async def get_chunk(
        chunk_id: Annotated[
            str,
            Field(
                description=(
                    "Chunk UUID from a chunk result id, a chunk evidence ref, or an entity "
                    "chunk-mention citation."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Exchange a chunk UUID for its TEXT (plus document provenance) —
        the id of a chunk result, a chunk evidence ref, or an entity chunk
        mention citation. Introspection shape (error/error_code). Row
        mention/evidence refs cite a structured table+pk and are not
        accepted here."""
        rt = _rt()
        # parsing needs no store — reject a malformed id BEFORE the binding
        # opens one (Codex #125 r3: a store outage must not mask this error)
        rejected = _invalid_chunk_payload(rt.context.project, chunk_id)
        if rejected is not None:
            return rejected
        bound_build: str | None = None
        try:
            async with asyncio.timeout(rt.policy.max_latency_ms / 1000.0):
                async with rt.context.bound() as deps:
                    bound_build = str(deps.repo.build_id)
                    return await _get_chunk(deps.repo, rt.context.project, chunk_id)
        except NoActiveBuildError:
            return _introspection_no_active_build(rt, chunk_id)
        except TimeoutError:
            return _introspection_timeout(rt, bound_build, chunk_id)
        except _STORE_ERRORS as exc:
            return _introspection_store_error(rt, bound_build, chunk_id, exc)

    @server.tool()
    async def get_document(
        document_id: Annotated[
            str, Field(description="Document UUID (e.g. a chunk's document_id).")
        ],
    ) -> dict[str, Any]:
        """Exchange a document UUID (a chunk's document_id) for its source
        provenance and full RAW content. Introspection shape
        (error/error_code)."""
        rt = _rt()
        # same pre-binding rejection as get_chunk (Codex #125 r3)
        rejected = _invalid_document_payload(rt.context.project, document_id)
        if rejected is not None:
            return rejected
        bound_build: str | None = None
        try:
            async with asyncio.timeout(rt.policy.max_latency_ms / 1000.0):
                async with rt.context.bound() as deps:
                    bound_build = str(deps.repo.build_id)
                    return await _get_document(
                        deps.repo, rt.context.project, document_id, rt.exposure
                    )
        except NoActiveBuildError:
            return _introspection_no_active_build(rt, document_id)
        except TimeoutError:
            return _introspection_timeout(rt, bound_build, document_id)
        except _STORE_ERRORS as exc:
            return _introspection_store_error(rt, bound_build, document_id, exc)

    @server.tool()
    async def list_entities(
        q: Annotated[
            str | None,
            Field(
                description=(
                    "Case-insensitive substring over the canonical name "
                    "(主館 finds 主題館); falls back to "
                    "character matching when a short probe finds nothing."
                )
            ),
        ] = None,
        entity_type: Annotated[
            str | None, Field(description="Filter by ontology type (as shown in results).")
        ] = None,
        limit: Annotated[int, Field(description="Page size, 1..200.")] = 50,
        cursor: Annotated[
            str | None,
            Field(
                description=(
                    "Continuation cursor from the previous page's next_cursor; restart without one "
                    "after an activation or filter change."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Browse or SEARCH the build's entities, paged. q is a
        case-insensitive substring over the canonical name (主館 finds
        主題館 — use this instead of get_entity when unsure of the exact
        name); entity_type filters the ontology type. Walk next_cursor for
        the full listing — nothing is unreachable. Introspection shape
        (error/error_code)."""
        rt = _rt()
        # QA5 D5: the substring filter is free text and reaches the same LIKE
        # predicate — validate before binding, for the same reason as
        # get_entity's name (a blank q is NOT rejected: it is the documented
        # "browse everything" case, unlike a blank exact-name lookup)
        # entity_type is the SIBLING free-text filter handed to the very same
        # page_entities() call — guarding only q would leave D5 reproducible
        # verbatim through the other parameter
        # q carries the browse cap (BROWSE_Q_CAP) so an oversized value is
        # refused on length before the O(n) storability scan runs (Codex #140
        # r7); entity_type has no length cap, so its scan — now C-speed — is the
        # only guard it needs.
        for param, value, cap in (("q", q, BROWSE_Q_CAP), ("entity_type", entity_type, None)):
            if value is None:
                continue
            unusable = _unusable_subject_payload(
                rt.context.project, param, value, allow_blank=True, max_len=cap
            )
            if unusable is not None:
                return unusable
        bound_build: str | None = None
        try:
            async with asyncio.timeout(rt.policy.max_latency_ms / 1000.0):
                async with rt.context.bound() as deps:
                    bound_build = str(deps.repo.build_id)
                    return await _list_entities(
                        deps.repo, rt.context.project, limit, cursor, q, entity_type
                    )
        except NoActiveBuildError:
            return _introspection_no_active_build(rt, q or "list_entities")
        except TimeoutError:
            return _introspection_timeout(rt, bound_build, q or "list_entities")
        except _STORE_ERRORS as exc:
            return _introspection_store_error(rt, bound_build, q or "list_entities", exc)

    @server.tool()
    async def list_chunks(
        limit: Annotated[int, Field(description="Page size, 1..200.")] = 50,
        cursor: Annotated[
            str | None,
            Field(description="Continuation cursor from the previous page's next_cursor."),
        ] = None,
    ) -> dict[str, Any]:
        """Browse the build's text chunks, paged, with a named-truncation
        text preview (full text via get_chunk). Walk next_cursor for the
        complete corpus — the retrieval top_k ceiling does not apply here.
        Introspection shape (error/error_code)."""
        rt = _rt()
        bound_build: str | None = None
        try:
            async with asyncio.timeout(rt.policy.max_latency_ms / 1000.0):
                async with rt.context.bound() as deps:
                    bound_build = str(deps.repo.build_id)
                    return await _list_chunks(deps.repo, rt.context.project, limit, cursor)
        except NoActiveBuildError:
            return _introspection_no_active_build(rt, "list_chunks")
        except TimeoutError:
            return _introspection_timeout(rt, bound_build, "list_chunks")
        except _STORE_ERRORS as exc:
            return _introspection_store_error(rt, bound_build, "list_chunks", exc)

    @server.tool()
    async def list_reports(
        limit: Annotated[int, Field(description="Page size, 1..200.")] = 50,
        cursor: Annotated[
            str | None,
            Field(description="Continuation cursor from the previous page's next_cursor."),
        ] = None,
    ) -> dict[str, Any]:
        """Browse the build's community reports, paged, each with its FULL
        summary (the global_summary retrieval ceiling hides most of them —
        this reaches ALL, content included). Introspection shape
        (error/error_code)."""
        rt = _rt()
        bound_build: str | None = None
        try:
            async with asyncio.timeout(rt.policy.max_latency_ms / 1000.0):
                async with rt.context.bound() as deps:
                    bound_build = str(deps.repo.build_id)
                    return await _list_reports(deps.repo, rt.context.project, limit, cursor)
        except NoActiveBuildError:
            return _introspection_no_active_build(rt, "list_reports")
        except TimeoutError:
            return _introspection_timeout(rt, bound_build, "list_reports")
        except _STORE_ERRORS as exc:
            return _introspection_store_error(rt, bound_build, "list_reports", exc)

    @server.tool()
    async def list_schema() -> dict[str, Any]:
        """The queryable structured surface (each whitelisted sql table
        with its live columns) PLUS this session's operative policy
        ceilings — max_top_k, max_graph_hops, max_latency_ms, query/browse caps,
        whether NL->SQL is enabled (parameterized graph_query is always
        available), expose_debug — so
        limits are discoverable up front instead of by tripping them.
        Introspection shape (error/error_code)."""
        return await _list_schema(_rt())

    @server.tool()
    async def explain_retrieval(
        query: Annotated[str, Field(description="The question, natural language.")],
        top_k: Annotated[
            int | None,
            Field(description="Max fused results; omitted = the policy ceiling."),
        ] = None,
    ) -> dict[str, Any]:
        """Run the hybrid router and return the response WITH its routing
        trace (the debug block). Gated by the operator's expose_debug
        policy flag: when it is off the call is REFUSED up front
        (GUARDRAIL_BLOCKED, zero results) — use hybrid_query for results
        without a trace."""
        rt = _rt()
        # the shared query cap runs FIRST (Codex #133 r1): this early return
        # bypasses _bounded, and _debug_disabled_payload echoes the query —
        # an unchecked oversized input would be reflected whole
        unusable = _unusable_query_payload(rt.context.project, "hybrid_query", query)
        if unusable is not None:
            return unusable
        if not rt.policy.expose_debug:
            return _debug_disabled_payload(rt.context.project, query)

        async def _run(deps: Any, remaining_ms: int) -> McpResponse:
            return await run_hybrid(
                deps,
                hybrid_policy(rt.policy, top_k, latency_budget_ms=remaining_ms),
                query,
                None,
            )

        payload = await _bounded(rt, "hybrid_query", query, _run)
        return _with_clamp_warning(payload, rt.policy, top_k)

    _finalize_server_metadata(server)
    _guard_tool_dispatch(server, project)
    # MCP16: the shared client bundle's close handle — called by the OWNER
    # of the server instance (the gateway host task after the child app's
    # lifespan exits, or run_server after a stdio run), never per session
    server._graphrag_close_shared = _close_shared  # type: ignore[attr-defined]
    return server


def _policy_disclosure(runtime: _Runtime) -> dict[str, Any]:
    """The session's ACTUAL policy, disclosed (MCP15). Built from the
    lifespan-loaded object alone — NO store is touched, so every list_schema
    branch (including no-active-build / timeout / store-error, exactly the
    degraded states where an agent most needs to inspect its session before
    retrying — Codex #135 r1) can carry it."""
    return {
        # default_mode is deliberately NOT disclosed (same rule as the
        # text_to_cypher toggle): no MCP dispatch path consumes it — the
        # initialize instructions designate hybrid_query as the default
        # entry — so disclosing it would hand agents routing guidance that
        # contradicts the instructions (Codex #135 r4). It returns if MCP
        # dispatch ever starts honoring it.
        "max_top_k": runtime.policy.max_top_k,
        "max_graph_hops": runtime.policy.max_graph_hops,
        # RECONCILED values throughout (Codex #135 r2): what is disclosed
        # must be what is ENFORCED — raw top-level fields overstate whenever
        # a project lowers a mode-local ceiling (rows) or a mode-local
        # timeout sits under max_latency_ms
        "max_sql_rows": runtime.policy.sql_rows(),
        "max_graph_rows": runtime.policy.cypher_policy().max_rows,
        "max_latency_ms": runtime.policy.max_latency_ms,
        "sql_timeout_ms": runtime.policy.sql_policy().timeout_ms,
        "graph_timeout_ms": runtime.policy.cypher_policy().timeout_ms,
        "expose_debug": runtime.policy.expose_debug,
        "sql_enabled": runtime.policy.text_to_sql.enabled,
        # text_to_cypher.enabled is deliberately NOT disclosed: it toggles
        # the OPTIONAL free NL(prose)->Cypher path, which no MCP surface
        # currently exposes (core/query/graph.py: no API in the chain
        # accepts query text) — any value would mislead (false read as
        # "graph unavailable" though parameterized graph_query always works;
        # true suggests a nonexistent free-form tool). The field returns
        # when the optional path ships (Codex #135 r3 + gate-2 residual).
        # response size has NO application-level limit — said explicitly
        # (null = unbounded), so "unbounded" is distinguishable from an
        # omitted setting without experimenting (Codex #135 r3; the task
        # names response size among the discoverable constraints)
        "max_response_bytes": None,
        "query_chars_cap": _QUERY_CHARS_CAP,
        "browse_limit_cap": BROWSE_LIMIT_CAP,
        "browse_q_cap": BROWSE_Q_CAP,
    }


async def _list_schema(runtime: _Runtime) -> dict[str, Any]:
    """§9 ``list_schema``: the whitelisted sql tables with their live columns
    (introspection shape). The wall clock covers binding + discovery; the
    STATEMENT deadline fires as a DB error (sqlstate 57014), not
    asyncio.TimeoutError — sql_query already maps this (§22) — and any other
    DB failure degrades with its class named rather than erroring the MCP
    call. A non-DB bug still propagates loud (never laundered as §22)."""
    bound_build: str | None = None
    try:
        async with asyncio.timeout(runtime.policy.max_latency_ms / 1000.0):
            async with runtime.context.bound() as deps:
                bound_build = str(deps.repo.build_id)
                tables: dict[str, list[str]] = {}
                if runtime.policy.text_to_sql.enabled:
                    # the same JSON-key discovery sql_query runs — under the
                    # same reconciled statement deadline (§21), plus the
                    # wall-clock bound around the whole call
                    async with deps.sql_reader.timed_transaction(
                        runtime.policy.sql_policy().timeout_ms
                    ):
                        columns = await deps.sql_reader.columns_by_table(
                            list(runtime.policy.text_to_sql.allowed_tables)
                        )
                    tables = {table: list(cols) for table, cols in columns.items()}
                return {
                    "project": runtime.context.project,
                    # the build these columns belong to — an activation
                    # between this lookup and a later sql_query would
                    # otherwise be undetectable by the caller (DR-001)
                    "build_id": bound_build,
                    "sql_enabled": runtime.policy.text_to_sql.enabled,
                    "tables": tables,
                    # MCP15: every ceiling an agent could previously learn
                    # only by tripping it (or reverse-engineering warnings)
                    # — and per-project policy divergence was INVISIBLE
                    # (measured: a policy edit changed behavior with no
                    # discoverable surface reflecting it). This block is the
                    # session's ACTUAL policy, read at lifespan start.
                    "policy": _policy_disclosure(runtime),
                    "error": None,
                    "error_code": None,
                }
    except NoActiveBuildError:
        # the policy rides EVERY branch (it needs no store): the degraded
        # states are exactly when an agent inspects its session (Codex #135)
        return {
            **_introspection_no_active_build(runtime, "list_schema"),
            "policy": _policy_disclosure(runtime),
        }
    except TimeoutError:
        return {
            **_introspection_timeout(runtime, bound_build, "list_schema"),
            "policy": _policy_disclosure(runtime),
        }
    except DBAPIError as exc:
        if _is_statement_timeout(exc):
            return {
                **_introspection_timeout(runtime, bound_build, "list_schema"),
                "policy": _policy_disclosure(runtime),
            }
        return {
            **_introspection_store_error(runtime, bound_build, "list_schema", exc),
            "policy": _policy_disclosure(runtime),
        }
    except _STORE_ERRORS as exc:
        # binding touches the other stores' clients too (qdrant/neo4j) — the
        # same §22 line as _bounded
        return {
            **_introspection_store_error(runtime, bound_build, "list_schema", exc),
            "policy": _policy_disclosure(runtime),
        }


async def _get_entity(repo: Any, project: str, name: str) -> dict[str, Any]:
    """§9 ``get_entity``: name → the matching ACTIVE entities, each cited by
    its SoR mentions (§27.2's spirit: an entity with zero mentions cannot be
    cited — surfaced as uncited rather than dropped, since this is
    introspection, not a retrieval result)."""
    if not isinstance(name, str) or not name.strip():
        return {
            "project": project,
            "build_id": str(repo.build_id),
            "name": name if isinstance(name, str) else repr(name),
            "error": "name must be a non-blank string",
            "error_code": "INVALID_INPUT",
            "entities": [],
        }
    entity_ids = await repo.entity_ids_by_name(name)
    # MCP7 (v1.1): mentions arrive RESOLVED — a chunk mention carries the
    # chunk UUID (get_chunk accepts it directly), source_uri, and the quote
    # + offsets; a row mention carries table+pk. The shared seam is
    # core/query/mentions.py; an unresolvable mention is omitted (§22) and
    # an entity may surface with zero mentions here (introspection shows
    # the uncited state rather than dropping the entity).
    refs_by_entity, _, _ = await resolved_mention_refs(repo, entity_ids, cap=None)
    return {
        "project": project,
        "build_id": str(repo.build_id),
        "name": name,
        "error": None,
        "error_code": None,
        "entities": [
            {
                "id": str(entity_id),
                "mentions": [
                    {
                        "source_type": ref.source_type,
                        "id": ref.id,
                        "source_uri": ref.source_uri,
                        "metadata": ref.metadata,
                    }
                    for ref in refs_by_entity.get(entity_id, ())
                ],
            }
            for entity_id in entity_ids
        ],
    }


#: Browse-page ceiling (MCP9): one introspection page tops out here — the
#: agent walks the cursor for more, so nothing is ever unreachable (the
#: max_top_k=20 retrieval ceiling made 73 of 93 reports and 422 of 442
#: chunks permanently invisible; browsing is how "this is ALL the options"
#: becomes answerable in principle).
BROWSE_LIMIT_CAP = 200

#: Chunk text preview length in list_chunks — browsing is for discovery,
#: get_chunk returns the full text; the truncation is NAMED per item.
_PREVIEW_CHARS = 200


def _browse_scope(tool: str, **facets: Any) -> str:
    """Canonical scope fingerprint of ONE browse result set (class 31 — the
    REST ``_scope_fingerprint`` pattern): sha256 over canonical JSON of the
    tool name and every facet that shapes the set (q, type, match mode), so
    no separator-injection edge exists and any axis change flips the tag."""
    payload = json.dumps({"tool": tool, **facets}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _browse_cursor(build_id: str, last_id: str, scope: str) -> str:
    """Mint a continuation cursor pinned to its full result-set identity
    (class 31): the ACTIVE build and the scope fingerprint — a cursor
    replayed after an activation, from another tool, or with different
    filters must be refused, never silently re-anchored."""
    return f"{build_id}|{scope}|{last_id}"


def _parse_browse_cursor(
    cursor: str, build_id: str, scope: str
) -> tuple[uuid.UUID | None, str | None]:
    """``(after_id, error)`` — refuses a cursor minted for another build or
    another listing scope, naming the cause (the agent re-lists instead of
    silently walking a MIXED result set)."""
    parts = cursor.split("|", 2)
    if len(parts) != 3:
        return None, "cursor is not a graphRAG browse cursor — start again without one"
    cursor_build, cursor_scope, last = parts
    if cursor_build != build_id:
        return None, (
            "cursor was minted for a different build (the active build changed) — "
            "restart the listing without a cursor"
        )
    if cursor_scope != scope:
        return None, (
            "cursor was minted for a different listing scope (another tool, other "
            "filters, or another match mode) — restart the listing without a cursor"
        )
    parsed = _parse_uuid(last)
    if parsed is None:
        return None, "cursor is corrupt — start again without one"
    return parsed, None


#: Search-string ceiling (Codex #129 r2): q is agent-controlled and the
#: character-AND fallback builds ONE ILIKE predicate per character — an
#: unbounded q would compile an enormous statement. 64 covers any real
#: name probe; the fuzzy fallback additionally engages only for short
#: (name-ish) queries.
BROWSE_Q_CAP = 64
FUZZY_Q_CAP = 16


def _bad_q(q: str | None) -> str | None:
    if q is not None and len(q) > BROWSE_Q_CAP:
        return f"q must be at most {BROWSE_Q_CAP} characters, got {len(q)}"
    return None


def _bad_limit(limit: Any) -> str | None:
    if type(limit) is not int or limit < 1 or limit > BROWSE_LIMIT_CAP:
        return f"limit must be an integer in 1..{BROWSE_LIMIT_CAP}, got {limit!r}"
    return None


async def _list_entities(
    repo: Any,
    project: str,
    limit: int,
    cursor: str | None,
    q: str | None,
    entity_type: str | None,
) -> dict[str, Any]:
    """§9 ``list_entities`` (MCP9): paged browse/search over ACTIVE entities.

    ``q`` is a case-insensitive SUBSTRING over canonical_name — the same
    semantics as the REST search, closing the exact-match dead end where a
    visitor's 主館 zeroed against the corpus's 主題館. ``next_cursor`` is
    scope-pinned (build + filters, class 31)."""
    build_id = str(repo.build_id)
    envelope = {"project": project, "build_id": build_id}
    bad = _bad_limit(limit) or _bad_q(q)
    if bad is not None:
        return {
            **envelope,
            "entities": [],
            "next_cursor": None,
            "error": bad,
            "error_code": "INVALID_INPUT",
        }
    # the match mode is STICKY across pages via the cursor scope (class 31):
    # substring first; when a fresh search finds nothing, fall back to
    # character-AND (主館 is not a substring of 主題館 — but 主 and 館 both
    # are), and the response NAMES which mode matched. On continuation the
    # mode is recovered by matching the cursor against either mode's
    # fingerprint — an unknown tag is refused, never guessed.
    match = "substring"
    after_id: uuid.UUID | None = None
    if cursor is not None:
        sub_scope = _entity_scope("substring", q, entity_type)
        chr_scope = _entity_scope("characters", q, entity_type)
        tag = cursor.split("|", 2)[1] if cursor.count("|") >= 2 else ""
        match = "characters" if tag == chr_scope else "substring"
        scope = chr_scope if match == "characters" else sub_scope
        after_id, cursor_error = _parse_browse_cursor(cursor, build_id, scope)
        if cursor_error is not None:
            return {
                **envelope,
                "entities": [],
                "next_cursor": None,
                "error": cursor_error,
                "error_code": "INVALID_INPUT",
            }
        rows = await repo.page_entities(
            limit + 1, after_id, q, entity_type, fuzzy=(match == "characters")
        )
    else:
        rows = await repo.page_entities(limit + 1, None, q, entity_type)
        if not rows and q and 2 <= len(q) <= FUZZY_Q_CAP:
            # the per-character fallback engages only for short (name-ish)
            # probes — each character costs one ILIKE predicate
            match = "characters"
            rows = await repo.page_entities(limit + 1, None, q, entity_type, fuzzy=True)
    scope = _entity_scope(match, q, entity_type)
    page = rows[:limit]
    next_cursor = (
        _browse_cursor(build_id, str(page[-1].id), scope) if len(rows) > limit and page else None
    )
    return {
        **envelope,
        "entities": [
            {"id": str(row.id), "name": row.canonical_name, "type": row.type} for row in page
        ],
        "match": match if q else None,
        "next_cursor": next_cursor,
        "error": None,
        "error_code": None,
    }


def _entity_scope(match: str, q: str | None, entity_type: str | None) -> str:
    return _browse_scope("list_entities", match=match, q=q or "", type=entity_type or "")


async def _list_chunks(repo: Any, project: str, limit: int, cursor: str | None) -> dict[str, Any]:
    """§9 ``list_chunks`` (MCP9): paged browse over the build's chunks with a
    NAMED-truncation text preview (get_chunk returns the full text)."""
    build_id = str(repo.build_id)
    envelope = {"project": project, "build_id": build_id}
    bad = _bad_limit(limit)
    if bad is not None:
        return {
            **envelope,
            "chunks": [],
            "next_cursor": None,
            "error": bad,
            "error_code": "INVALID_INPUT",
        }
    after_id: uuid.UUID | None = None
    if cursor is not None:
        after_id, cursor_error = _parse_browse_cursor(
            cursor, build_id, _browse_scope("list_chunks")
        )
        if cursor_error is not None:
            return {
                **envelope,
                "chunks": [],
                "next_cursor": None,
                "error": cursor_error,
                "error_code": "INVALID_INPUT",
            }
    columns = (
        tables.chunks.c.id,
        tables.chunks.c.document_id,
        tables.chunks.c.ordinal,
        tables.chunks.c.text,
    )
    rows = await repo.page_rows(tables.chunks, columns, limit + 1, after_id)
    page = rows[:limit]
    next_cursor = (
        _browse_cursor(build_id, str(page[-1].id), _browse_scope("list_chunks"))
        if len(rows) > limit and page
        else None
    )
    return {
        **envelope,
        "chunks": [
            {
                "id": str(row.id),
                "document_id": str(row.document_id),
                "ordinal": row.ordinal,
                "text_preview": (row.text or "")[:_PREVIEW_CHARS],
                "text_truncated": bool(row.text) and len(row.text) > _PREVIEW_CHARS,
            }
            for row in page
        ],
        "next_cursor": next_cursor,
        "error": None,
        "error_code": None,
    }


async def _list_reports(repo: Any, project: str, limit: int, cursor: str | None) -> dict[str, Any]:
    """§9 ``list_reports`` (MCP9): paged browse over community reports —
    73 of 93 were permanently invisible under the retrieval ceiling."""
    build_id = str(repo.build_id)
    envelope = {"project": project, "build_id": build_id}
    bad = _bad_limit(limit)
    if bad is not None:
        return {
            **envelope,
            "reports": [],
            "next_cursor": None,
            "error": bad,
            "error_code": "INVALID_INPUT",
        }
    after_id: uuid.UUID | None = None
    if cursor is not None:
        after_id, cursor_error = _parse_browse_cursor(
            cursor, build_id, _browse_scope("list_reports")
        )
        if cursor_error is not None:
            return {
                **envelope,
                "reports": [],
                "next_cursor": None,
                "error": cursor_error,
                "error_code": "INVALID_INPUT",
            }
    columns = (
        tables.community_reports.c.id,
        tables.community_reports.c.title,
        tables.community_reports.c.summary,
        tables.community_reports.c.rating,
    )
    rows = await repo.page_rows(tables.community_reports, columns, limit + 1, after_id)
    page = rows[:limit]
    next_cursor = (
        _browse_cursor(build_id, str(page[-1].id), _browse_scope("list_reports"))
        if len(rows) > limit and page
        else None
    )
    return {
        **envelope,
        "reports": [
            {
                "id": str(row.id),
                "title": row.title if isinstance(row.title, str) else None,
                # the FULL summary rides along (Codex #129 P1): there is no
                # get_report tool and global_summary cannot select by id, so
                # omitting it here would leave beyond-ceiling report content
                # permanently unreachable — the exact wall MCP9 removes
                "summary": row.summary if isinstance(row.summary, str) else None,
                "rating": row.rating,
            }
            for row in page
        ],
        "next_cursor": next_cursor,
        "error": None,
        "error_code": None,
    }


#: get_chunk's invalid-id message — NAMES the mention-ref shape (the MCP7
#: gap) instead of a bare "invalid" (#124: no dead ends). One constant, two
#: emitters: the helper (direct callers) and the pre-binding wrapper check.
_CHUNK_ID_MESSAGE = (
    "chunk_id must be a chunk UUID (the id of a chunk result, a chunk "
    "evidence ref, or an entity CHUNK mention ref — those carry chunk UUIDs "
    "since v1.1; row mention/evidence refs cite table+pk and are not chunks); "
    "a raw chunk:{content_hash}:{ordinal} string is the STORED form and is "
    "not accepted"
)

#: get_document's invalid-id message (same two-emitter single source).
_DOCUMENT_ID_MESSAGE = "document_id must be a document UUID (a chunk's document_id field)"


def _parse_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError):
        return None


def _incomplete_graph_invocation_payload(
    project: str,
    query: str,
    *,
    graph_template: str | None,
    graph_entity: str | None,
    graph_other_entity: str | None,
    graph_hops: int | None,
) -> dict[str, Any] | None:
    """MCP11: a caller-supplied graph invocation is TEMPLATE + ENTITY; any
    ``graph_*`` argument without that complete pair used to be dropped
    SILENTLY — worse, the QP1 auto plan would then run the router's OWN
    template/seed and the agent believed THEIRS ran. The tool docstring
    promises "run YOUR graph invocation", so a partial one is a client
    error refused LOUDLY before binding (parsing needs no store — the same
    pre-binding convention as :func:`_invalid_chunk_payload`; nil build,
    nothing spent): the router must never trust its own guess over the
    caller's explicit instruction. Returns None when the invocation is
    COMPLETE (template + entity, with other_entity/hops as optional
    refinements) or ABSENT (no graph_* argument at all — QP1 may plan
    freely)."""
    supplied = [
        name
        for name, value in (
            ("graph_template", graph_template),
            ("graph_entity", graph_entity),
            ("graph_other_entity", graph_other_entity),
            ("graph_hops", graph_hops),
        )
        if value is not None
    ]
    if not supplied or (graph_template is not None and graph_entity is not None):
        return None
    missing = [name for name in ("graph_template", "graph_entity") if name not in supplied]
    return McpResponse(
        # a WITHIN-cap query is echoed whole (clients correlate/log/retry
        # from the §16 envelope — Codex #133 r2); only a genuinely oversized
        # input is truncated, the defense-in-depth backstop should the
        # tool's cap-first ordering ever regress (Codex #133 r1 class)
        query=_echoable(query),
        tool="hybrid_query",
        project=project,
        build_id=_NIL_BUILD,
        results=(),
        warnings=(
            QueryWarning(
                "GUARDRAIL_BLOCKED",
                f"incomplete graph invocation — {supplied} supplied without {missing}: "
                "a caller-supplied graph invocation needs BOTH graph_template AND "
                "graph_entity (graph_other_entity / graph_hops are optional "
                "refinements). Refused instead of silently running the router's own "
                "auto plan — your graph invocation did NOT run; supply the missing "
                "parameter(s) and retry, or omit every graph_* parameter to let the "
                "router plan",
            ),
        ),
    ).to_dict()


def _debug_disabled_payload(project: str, query: str) -> dict[str, Any]:
    """MCP13 (b): GUARDRAIL_BLOCKED means "refused, nothing produced"
    EVERYWHERE else (hops=99 / unknown template → n=0), but explain_retrieval
    used to run the FULL pipeline (measured 5.4s + real LLM spend), return a
    SUCCESSFUL page, and stamp it GUARDRAIL_BLOCKED — an agent applying the
    uniform rule would discard a good answer, and one applying the local
    reading forks the code's meaning. The flag is known at lifespan, so the
    call is refused BEFORE binding: one code, one meaning, no wasted
    pipeline. Nil build — nothing was ever resolved (pre-binding
    convention)."""
    return McpResponse(
        # within-cap echoed whole, oversized truncated — same rule as the
        # incomplete-invocation refusal (Codex #133 r2)
        query=_echoable(query),
        tool="hybrid_query",
        project=project,
        build_id=_NIL_BUILD,
        results=(),
        warnings=(
            QueryWarning(
                "GUARDRAIL_BLOCKED",
                "expose_debug is disabled by policy — explain_retrieval refused "
                "before running the query (nothing was produced; §21). Use "
                "hybrid_query for results without a trace, or ask the operator "
                "to enable expose_debug",
            ),
        ),
    ).to_dict()


def _invalid_chunk_payload(project: str, chunk_id: str) -> dict[str, Any] | None:
    """Typed rejection for a malformed chunk_id BEFORE store binding, or None
    when the id parses (Codex #125 r3): parsing needs no store, so a bad id
    must never open the binding — and when a store is down the caller still
    gets the actionable UUID error, not STORE_UNAVAILABLE. ``build_id`` is
    the nil sentinel: no build was ever resolved (the same convention as the
    pre-binding timeout envelope)."""
    if _parse_uuid(chunk_id) is not None:
        return None
    return {
        "project": project,
        "build_id": _NIL_BUILD,
        "chunk_id": _safe_echo(chunk_id),
        "chunk": None,
        "error": _CHUNK_ID_MESSAGE,
        "error_code": "INVALID_INPUT",
    }


def _unusable_subject_payload(
    project: str,
    subject_field: str,
    subject: str,
    *,
    allow_blank: bool = False,
    max_len: int | None = None,
) -> dict[str, Any] | None:
    """Typed rejection for an introspection argument that cannot be served AS
    GIVEN — a NUL / unpaired surrogate, or nothing but whitespace — BEFORE
    store binding, or None when it is usable (QA5; the ``_invalid_chunk_payload``
    bind-then-validate precedent, Codex #125 r3).

    Without it a NUL rode into Postgres and returned a ``DBAPIError``, which
    the §22 handler faithfully relabelled ``STORE_UNAVAILABLE`` "postgres
    unavailable" (D5) — the server telling a contract-abiding agent to back
    off from a database that was perfectly healthy. Validating here costs no
    store read and keeps the two answers honest: a bad ARGUMENT is
    INVALID_INPUT, an unreachable store is STORE_UNAVAILABLE.

    ``allow_blank`` marks a subject where emptiness is MEANINGFUL rather than
    a mistake — ``list_entities(q=...)`` browses everything without a filter,
    unlike an exact-name lookup, where blank can only be a bug.

    ``max_len`` caps the subject BEFORE the storability scan (Codex #140 r7):
    that scan is O(n) per character, and the MCP input schema imposes no length
    bound, so an oversized value that will always be refused must be rejected on
    length (O(1)) before it can drive an O(n) surrogate/NUL walk — the same
    length-first ordering ``_unusable_query_payload`` gives the retrieval query.
    ``None`` means the subject has no cap (an exact-name lookup)."""
    if max_len is not None and len(subject) > max_len:
        return {
            "project": project,
            "build_id": _NIL_BUILD,
            "subject": _safe_echo(subject),
            "error": (
                f"{subject_field} must be at most {max_len} characters, "
                f"got {len(subject)} — this is an INPUT problem, not a store outage"
            ),
            "error_code": "INVALID_INPUT",
        }
    reason = _unusable_text_reason(subject, allow_blank=allow_blank)
    if reason is None:
        return None
    return {
        "project": project,
        "build_id": _NIL_BUILD,
        "subject": _safe_echo(subject),
        "error": (f"{subject_field} {reason} — this is an INPUT problem, not a store outage"),
        "error_code": "INVALID_INPUT",
    }


def _invalid_document_payload(project: str, document_id: str) -> dict[str, Any] | None:
    """get_document's pre-binding twin of :func:`_invalid_chunk_payload`."""
    if _parse_uuid(document_id) is not None:
        return None
    return {
        "project": project,
        "build_id": _NIL_BUILD,
        "document_id": _safe_echo(document_id),
        "document": None,
        "error": _DOCUMENT_ID_MESSAGE,
        "error_code": "INVALID_INPUT",
    }


async def _get_chunk(repo: Any, project: str, chunk_id: str) -> dict[str, Any]:
    """§9 ``get_chunk``: chunk UUID → its text + provenance from the SoR.

    THE citation-to-content path (MCP5): a chunk result's id, a chunk
    evidence ref, and — since v1.1 (MCP7) — an entity CHUNK mention
    citation all carry chunk UUIDs this helper resolves; row mention/
    evidence refs cite a structured table+pk and are not chunks. Accepts
    only the UUID form: the raw ``chunk:{content_hash}:{ordinal}`` string
    is the STORED mention format (never emitted since v1.1), and the error
    NAMES that instead of a bare "invalid" so an agent holding a stale one
    learns why it cannot be used. Validation also runs pre-binding in the
    tool wrapper; it repeats here so DIRECT callers get the same guarantee.
    """
    envelope = {"project": project, "build_id": str(repo.build_id), "chunk_id": chunk_id}
    parsed = _parse_uuid(chunk_id)
    if parsed is None:
        return {
            **envelope,
            "chunk": None,
            "error": _CHUNK_ID_MESSAGE,
            "error_code": "INVALID_INPUT",
        }
    rows = await repo.fetch_all(tables.chunks, tables.chunks.c.id == parsed)
    if not rows:
        return {
            **envelope,
            "chunk": None,
            "error": "no chunk with this id in the ACTIVE build",
            "error_code": "NOT_FOUND",
        }
    row = rows[0]
    return {
        **envelope,
        "chunk": {
            "id": str(row.id),
            "document_id": str(row.document_id),
            "ordinal": row.ordinal,
            "text": row.text,
            "start_offset": row.start_offset,
            "end_offset": row.end_offset,
            "token_count": row.token_count,
        },
        "error": None,
        "error_code": None,
    }


async def _get_document(
    repo: Any, project: str, document_id: str, exposure: MetadataExposure
) -> dict[str, Any]:
    """§9 ``get_document``: document UUID → provenance + the full RAW content.

    The document-level half of MCP5. ``raw`` is emitted whole, matching the
    REST detail contract ("returned on detail GET only") — an agent calling
    this tool is explicitly asking for the source document; silently
    truncating it would misrepresent the corpus (§22's silence rule).
    ``ingested_at`` is stringified: introspection payloads are plain JSON,
    there is no FastAPI encoder here. ``metadata`` is NOT the stored
    envelope: DR-010 makes stored metadata agent-invisible unless the
    project's ``metadata_exposure`` allowlist names it, so the envelope is
    projected through the SAME fail-closed ``MetadataExposure.project`` the
    retrieval enrichment path uses (empty allowlist → empty object)."""
    envelope = {"project": project, "build_id": str(repo.build_id), "document_id": document_id}
    parsed = _parse_uuid(document_id)
    if parsed is None:
        return {
            **envelope,
            "document": None,
            "error": _DOCUMENT_ID_MESSAGE,
            "error_code": "INVALID_INPUT",
        }
    rows = await repo.fetch_all(tables.documents, tables.documents.c.id == parsed)
    if not rows:
        return {
            **envelope,
            "document": None,
            "error": "no document with this id in the ACTIVE build",
            "error_code": "NOT_FOUND",
        }
    row = rows[0]
    return {
        **envelope,
        "document": {
            "id": str(row.id),
            "source_uri": row.source_uri,
            "mime": row.mime,
            "content_hash": row.content_hash,
            "metadata": exposure.project(row.metadata or {}),
            "ingested_at": str(row.ingested_at) if row.ingested_at is not None else None,
            "status": row.status,
            "raw": row.raw,
        },
        "error": None,
        "error_code": None,
    }


#: §9's user-facing transport vocabulary → the SDK's transport names. "http"
#: is streamable HTTP (the MCP spec's current HTTP transport); SSE is the
#: SDK's legacy HTTP flavor and deliberately NOT offered — one HTTP transport,
#: no ambiguity for the consuming platform.
TRANSPORTS: Final[dict[str, str]] = {"stdio": "stdio", "http": "streamable-http"}


def run_server(server: FastMCP, transport: str = "stdio") -> None:
    """Run a built server on a §9 transport — the one place the vocabulary is
    mapped, so every project entrypoint offers the same choices. Unknown
    names fail loud (a typo'd transport must never silently fall back to
    stdio and strand the HTTP consumer)."""
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r} (choose from {sorted(TRANSPORTS)})")
    try:
        server.run(transport=cast(Any, TRANSPORTS[transport]))
    finally:
        # MCP16: sessions no longer close the shared client bundle — the
        # server's OWNER does; for a direct run that owner is this function.
        # Best-effort: the clients were bound to server.run's now-gone loop,
        # so a loop-bound close may complain — at process exit the OS
        # reclaims either way, and an ugly close must not mask the run's
        # own outcome (gate-2 nit).
        closer = getattr(server, "_graphrag_close_shared", None)
        if closer is not None:
            with contextlib.suppress(Exception):  # best-effort shutdown release
                asyncio.run(closer())


async def run_bounded_query(
    context: ProjectContext,
    policy: QueryPolicy,
    tool: str,
    query: str,
    runner: Any,
    exposure: MetadataExposure | None = None,
) -> dict[str, Any]:
    """Public seam for non-MCP facades (the Console query playground, BA6):
    the SAME §21 wall-clock deadline + per-call binding + §22 typed
    degradation envelope every MCP tool runs under — one machinery, two
    facades, so the REST playground can never drift from the MCP tools
    (class 5). ``runner(deps, remaining_ms) -> McpResponse`` exactly as the
    tools pass it; the returned dict is the §16 shape (the REST layer
    reprojects it onto the frozen QueryResult). ``exposure`` is the caller's
    document-metadata allowlist (the Console reads it from ``projects.config``);
    None is the fail-closed empty allowlist."""
    runtime = _Runtime(
        context=context,
        policy=policy,
        exposure=exposure or MetadataExposure(fields=()),
    )
    return await _bounded(runtime, tool, query, runner)
