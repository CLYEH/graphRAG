"""Shared fixtures. Integration tests are gated on the docker compose stack being up."""

from __future__ import annotations

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


@pytest.fixture(scope="session")
def require_services() -> None:
    """Skip an integration test unless the docker compose stack is reachable."""
    if not _services_up():
        pytest.skip("docker compose services not reachable — run: docker compose up -d")
