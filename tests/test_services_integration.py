"""Integration smoke — auto-skips when services are down. Real store checks land with C1."""

from __future__ import annotations

import socket

import pytest

pytestmark = pytest.mark.integration


def test_postgres_port_open(require_services: None) -> None:
    with socket.create_connection(("localhost", 5432), timeout=1.0):
        pass
