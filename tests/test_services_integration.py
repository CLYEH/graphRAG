"""Integration smoke — auto-skips when services are down. Real store checks land with C1."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import pytest

from core.config import get_settings

pytestmark = pytest.mark.integration


def test_postgres_port_open(require_services: None) -> None:
    # derive from settings, not a hardcoded port: compose maps a non-default host
    # port (15432) to dodge natively installed PostgreSQL squatting 5432. The
    # fallback mirrors the driver default (5432) — where a port-less DSN connects.
    dsn = urlparse(get_settings().postgres_dsn)
    with socket.create_connection((dsn.hostname or "localhost", dsn.port or 5432), timeout=1.0):
        pass
