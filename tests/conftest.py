"""Shared fixtures. Integration tests are gated on the docker compose stack being up."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from core.config import get_settings
from core.stores import tables


async def ensure_project(conn: AsyncConnection, name: str) -> None:
    """Seed a registry `projects` row so a `builds.insert()` (and other
    project-FK-backed inserts) can reference it. Idempotent — integration tests
    that mint ad-hoc builds call this first now that `builds.project` FKs
    `projects.name` (BA2b)."""
    await conn.execute(
        pg_insert(tables.projects).values(name=name).on_conflict_do_nothing(index_elements=["name"])
    )


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _services_up() -> bool:
    settings = get_settings()
    postgres = urlparse(settings.postgres_dsn)
    qdrant = urlparse(settings.qdrant_url)
    targets: list[tuple[str, int]] = [
        # port fallback = the DRIVER's default (5432): a port-less DSN connects there,
        # not to the compose host mapping (15432, always explicit in our DSNs)
        (postgres.hostname or "localhost", postgres.port or 5432),  # postgres
        ("localhost", 7687),  # neo4j bolt
        (qdrant.hostname or "localhost", qdrant.port or 6333),  # qdrant
        ("localhost", 6379),  # redis
    ]
    return all(_reachable(host, port) for host, port in targets)


def _gate_on_services() -> None:
    """Skip locally when the stack is down; FAIL in CI (fail loud).

    In CI the `integration` job is a required check — if unreachable services
    merely skipped, the check would go green having tested nothing.
    """
    if _services_up():
        return
    msg = "docker compose services not reachable — run: docker compose up -d"
    if os.environ.get("CI"):
        pytest.fail(f"{msg} (CI must not silently skip integration tests)")
    pytest.skip(msg)


@pytest.fixture(scope="session")
def require_services() -> None:
    """Skip an integration test unless the docker compose stack is reachable."""
    _gate_on_services()


# ---- H11: survive a LIVED-IN dev environment --------------------------------
#
# CI gets fresh services; a dev machine does not. Interrupted runs COMMIT
# rows/collections (teardowns never ran), and whole-table assumptions break.
# Two defenses live here: (1) a session-start sweep that deletes leftovers
# from PREVIOUS killed runs — running at the NEXT session start is the only
# hook that survives any kill signal; (2) test-scoped prefixes so the sweep
# can never touch a real project (nmmst/museum/...).

#: every prefix integration tests mint ad-hoc projects under — additions to
#: test files MUST register here or their leftovers survive kills forever
TEST_PROJECT_PREFIXES: tuple[str, ...] = (
    "itest-",
    "lifec-",
    "evalrun-",
    "sqltest-",
    "graphq-",
    "hybtest-",
    "vtest-",
    "xtest-",
    "qtest-",
    "globtest-",
    "mcptest-",
    "sumtest-",
    "obs-",
    "health-",
    "gtest-",
    "uxc1b-",
)

#: deletion order honors the FK graph: children without a project column
#: cascade from their parents (documents→chunks, entities→mentions,
#: relations→evidence, pipeline_runs→steps→items); builds→projects is
#: RESTRICT so builds go before projects
_SWEEP_TABLES: tuple[str, ...] = (
    "relations",
    "entities",
    "documents",
    "community_reports",
    "merge_candidates",
    "ontology_proposals",
    "pipeline_runs",
    "review_ledger",
    "idempotency_keys",
    "jobs",
    "sources",
    "builds",
)


@pytest.fixture(scope="session", autouse=True)
def sweep_stale_test_leftovers() -> None:
    """Delete rows/collections stale test runs left behind (H11).

    Autouse + session-scoped: runs once before the first test of any session,
    integration or not — but does nothing when the stack is down (the fast
    tier must stay hermetic and service-free). Scoped STRICTLY to
    TEST_PROJECT_PREFIXES, so real projects are structurally unreachable.
    """
    if not _services_up():
        return
    import asyncio

    from qdrant_client import QdrantClient
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def _sweep_postgres() -> None:
        settings = get_settings()
        dsn = settings.postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                likes = " OR ".join(
                    f"project LIKE :p{i}" for i in range(len(TEST_PROJECT_PREFIXES))
                )
                params = {f"p{i}": f"{prefix}%" for i, prefix in enumerate(TEST_PROJECT_PREFIXES)}

                async def _table_exists(name: str) -> bool:
                    # CI's integration job starts from a FRESH database with no
                    # migrate step before pytest — the sweep must no-op per
                    # missing table, never error the whole session (the
                    # lived-in dev DB being migrated is NOT an invariant)
                    return (
                        await conn.execute(text("SELECT to_regclass(:t)"), {"t": f"public.{name}"})
                    ).scalar_one() is not None

                for table in _SWEEP_TABLES:
                    if not await _table_exists(table):
                        continue
                    await conn.execute(text(f"DELETE FROM {table} WHERE {likes}"), params)  # noqa: S608 — table names from a frozen tuple, prefixes bound as parameters
                if await _table_exists("projects"):
                    name_likes = likes.replace("project LIKE", "name LIKE")
                    await conn.execute(text(f"DELETE FROM projects WHERE {name_likes}"), params)  # noqa: S608
                await conn.commit()
        finally:
            await engine.dispose()

    asyncio.run(_sweep_postgres())

    # Qdrant: eval/lifecycle runs orphan one collection per run
    # (project_<name>_<hash> — core/stores/vectors.py); sweep by prefix
    client = QdrantClient(url=get_settings().qdrant_url)
    try:
        for collection in client.get_collections().collections:
            name = collection.name
            if any(name.startswith(f"project_{p}") for p in TEST_PROJECT_PREFIXES):
                client.delete_collection(name)
    finally:
        client.close()
