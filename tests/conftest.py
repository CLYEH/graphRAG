"""Shared fixtures. Integration tests are gated on the docker compose stack being up."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest

from core.config import get_settings


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _services_up() -> bool:
    settings = get_settings()
    qdrant = urlparse(settings.qdrant_url)
    targets: list[tuple[str, int]] = [
        ("localhost", 5432),  # postgres
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
