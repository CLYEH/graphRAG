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


def main() -> None:
    """Console-script entrypoint."""
    args = _parser().parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
