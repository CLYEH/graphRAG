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
