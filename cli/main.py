"""graphRAG CLI (§14): build lifecycle administration.

``graphrag builds|activate|rollback|diff|prune <project>`` — thin argparse
shells over :mod:`core.builds.lifecycle` (the admin surface). Ingest/build
pipeline subcommands land with their tracks; this module stays a dispatcher:
every behavior worth testing lives in core.

Exit code: 0 on success, 1 when the operation reports failure (preflight
refused, nothing to roll back to) — scripts can gate on it (Rule 12: a
refused activation is a FAILURE exit, never a quiet 0).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.builds import lifecycle
from core.config import get_settings
from core.stores.graph import graph_driver
from core.stores.vectors import vector_client


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphrag", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    builds = sub.add_parser("builds", help="list a project's builds, newest first")
    builds.add_argument("project")

    activate = sub.add_parser("activate", help="preflight + promote a ready build (§14)")
    activate.add_argument("project")
    activate.add_argument("build_id", type=uuid.UUID)

    rollback = sub.add_parser("rollback", help="re-activate the previously active build")
    rollback.add_argument("project")

    diff = sub.add_parser("diff", help="row-count delta per table between two builds")
    diff.add_argument("project")
    diff.add_argument("build_a", type=uuid.UUID)
    diff.add_argument("build_b", type=uuid.UUID)

    evalp = sub.add_parser("eval", help="score a build against eval/golden.yaml (§20)")
    evalp.add_argument("project")
    evalp.add_argument(
        "--build", type=uuid.UUID, default=None, help="build to score (default: the active build)"
    )
    evalp.add_argument(
        "--golden",
        type=Path,
        default=None,
        help="golden set path (default: projects/<project>/eval/golden.yaml)",
    )
    evalp.add_argument(
        "--config",
        type=Path,
        default=None,
        help="OVERRIDE policy file (default: the registry projects.config — CFG1 one SoR)",
    )

    serve_mcp = sub.add_parser(
        "serve-mcp",
        help="serve EVERY project's MCP over streamable HTTP at /mcp/<project> (CFG1 gateway)",
    )
    serve_mcp.add_argument(
        "--host",
        default=None,
        help=(
            "bind host (default: core.config). NOTE: the Console's MCP panel advertises the "
            "SETTINGS address (GRAPHRAG_MCP_HTTP_HOST), never this override — set the setting "
            "so operators copy a URL that reaches this gateway"
        ),
    )
    serve_mcp.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "bind port (default: core.config). Same caveat as --host: the Console advertises "
            "the SETTINGS port"
        ),
    )

    prune = sub.add_parser("prune", help="GC builds beyond the retention window")
    prune.add_argument("project")
    prune.add_argument(
        "--keep",
        type=int,
        default=None,
        help="builds to keep (default: retention.keep_builds from settings)",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_async_engine(
        settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
        poolclass=NullPool,
    )
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with engine.connect() as conn, driver.session() as session:
            if args.command == "builds":
                for build in await lifecycle.list_builds(conn, args.project):
                    marker = "*" if build.status == "active" else " "
                    print(f"{marker} {build.id}  {build.status:9s}  started={build.started_at}")
                return 0
            if args.command == "activate":
                report = await lifecycle.activate(
                    conn, qdrant, session, args.project, args.build_id
                )
                return _print_report(report)
            if args.command == "rollback":
                target, report = await lifecycle.rollback(conn, qdrant, session, args.project)
                if target is not None and report.ok:
                    print(f"rolled back to {target}")
                return _print_report(report)
            if args.command == "diff":
                try:
                    table_diff = await lifecycle.diff(
                        conn, args.project, args.build_a, args.build_b
                    )
                except ValueError as exc:
                    print(f"REFUSED: {exc}", file=sys.stderr)
                    return 1
                for table, counts in table_diff.items():
                    print(
                        f"{table:20s} a={counts['a']:8d}  b={counts['b']:8d}  "
                        f"delta={counts['delta']:+d}"
                    )
                return 0
            if args.command == "eval":
                from core.eval.golden import GoldenError, load_golden
                from core.eval.runner import models_needed, run_eval
                from core.llm.factory import LLMNotConfiguredError, chat_model, embedding_model
                from core.mcp.policy import (
                    PolicyError,
                    load_query_policy,
                    load_query_policy_from_registry,
                )
                from core.stores.repo import active_build_id

                root = Path("projects") / args.project
                try:
                    golden = load_golden(args.golden or root / "eval" / "golden.yaml")
                    if args.config:
                        # explicit file override — the escape hatch; DEFAULT is
                        # the registry, the ONE policy SoR (CFG1)
                        policy = load_query_policy(args.config)
                    else:
                        # policy ONLY — eval never touches metadata exposure, and a
                        # malformed exposure block must not block scoring (#93 R2)
                        policy = await load_query_policy_from_registry(conn, args.project)
                        await conn.rollback()  # end the read txn like the build lookup below
                except (GoldenError, PolicyError) as exc:
                    print(f"REFUSED: {exc}", file=sys.stderr)
                    return 1
                try:
                    # only the model clients the golden set's modes will
                    # actually call — a graph-only golden set must evaluate
                    # without an API key; an unconfigured-but-needed model
                    # is a REFUSAL, never a traceback
                    needs_embedder, needs_llm = models_needed(golden, policy)
                    embedder = embedding_model() if needs_embedder else None
                    llm = chat_model() if needs_llm else None
                    target_build = args.build or await active_build_id(conn, args.project)
                    await conn.rollback()  # end the lookup's read txn
                    eval_report = await run_eval(
                        conn,
                        qdrant,
                        session,
                        embedder,
                        llm,
                        args.project,
                        target_build,
                        golden,
                        policy,
                    )
                except (LookupError, LLMNotConfiguredError) as exc:
                    print(f"REFUSED: {exc}", file=sys.stderr)
                    return 1
                for case_result in eval_report.cases:
                    mark = "PASS" if case_result.passed else "FAIL"
                    note = f"  ({case_result.note})" if case_result.note else ""
                    print(
                        f"{mark}  {case_result.score:.3f}  "
                        f"[{case_result.mode}] {case_result.question}{note}"
                    )
                print(
                    f"score={eval_report.score:.4f}  "
                    f"passed={eval_report.passed}  failed={eval_report.failed}"
                )
                return 0 if eval_report.failed == 0 else 1
            if args.command == "prune":
                keep = args.keep if args.keep is not None else settings.retention_keep_builds
                victims = await lifecycle.prune(conn, qdrant, session, args.project, keep=keep)
                print(f"pruned {len(victims)} build(s)" + (":" if victims else ""))
                for victim in victims:
                    print(f"  {victim}")
                return 0
            raise AssertionError(f"unhandled command {args.command}")  # argparse guards this
    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()


def _print_report(report: lifecycle.PreflightReport) -> int:
    for failure in report.failures:
        print(f"REFUSED: {failure}", file=sys.stderr)
    for deferred in report.deferred:
        print(f"deferred: {deferred}", file=sys.stderr)
    if report.ok:
        print("ok")
        return 0
    return 1


def _serve_mcp(args: argparse.Namespace) -> int:
    """CFG1 gateway: every project at ``/mcp/<project>`` on ONE port —
    uvicorn hosts the ASGI dispatcher; host/port ride core.config unless
    overridden. Runs until interrupted (it IS the server, not a lifecycle
    command, so it branches before ``_run``'s store plumbing)."""
    import uvicorn

    from core.mcp.gateway import build_gateway

    settings = get_settings()
    host = args.host or settings.mcp_http_host
    port = args.port or settings.mcp_http_port
    # The Console's MCP panel (GET /projects/{project}/mcp) derives the URL it
    # advertises from the SETTINGS alone — the frozen contract says so. A CLI
    # override therefore FORKS the source: the gateway listens here while the
    # Console keeps handing operators the settings address, and the copied link
    # reaches nothing. Warn loudly and name both addresses rather than let that
    # divergence be silent (it cannot be an error: overriding is legitimate for
    # one-off local runs).
    if host != settings.mcp_http_host or port != settings.mcp_http_port:
        print(
            f"warning: serving on {host}:{port}, but the Console advertises "
            f"{settings.mcp_http_host}:{settings.mcp_http_port} (from settings). "
            "Set GRAPHRAG_MCP_HTTP_HOST/GRAPHRAG_MCP_HTTP_PORT instead so the "
            "Console's MCP panel shows a URL that reaches this gateway.",
            file=sys.stderr,
        )
    uvicorn.run(build_gateway(), host=host, port=port)
    return 0


def main() -> None:
    """Console-script entrypoint."""
    args = _parser().parse_args()
    if args.command == "serve-mcp":
        sys.exit(_serve_mcp(args))
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
