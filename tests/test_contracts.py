"""Contract tests — validate frozen schemas. Skip until contracts/ is populated (Track 0)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.contract

_MCP_SCHEMA = Path(__file__).resolve().parent.parent / "contracts" / "mcp_response.schema.json"


@pytest.mark.skipif(not _MCP_SCHEMA.exists(), reason="contract not frozen yet (Track 0 P1)")
def test_mcp_response_schema_is_valid() -> None:
    schema = json.loads(_MCP_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
