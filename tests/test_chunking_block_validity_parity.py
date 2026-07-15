"""Why: the Console settings page decides whether an existing chunking block is
repairable-clean (show its salvaged form, no repair needed) or MALFORMED (a
build-blocking block whose corrected form must be saved with no dummy knob edit)
with a CLIENT-side predicate (web/src/api/queries.ts isValidChunkingBlock). The
authority on that verdict is this server's load_build_config
(core/builds/config.py _load_chunking). When the two drift, a hand-written block
the server rejects looks clean in the form, its salvaged {max_chars, overlap} is
never written, and every build stays blocked (Codex #79 R8 — the chunking
sibling of the query_policy / ontology bricks).

Parity is enforced mechanically from one corpus both suites read
(tests/fixtures/chunking_block_validity.json — the vitest half is
web/src/api/chunkingValidityParity.test.ts). This half runs the REAL loader,
wrapping each block under the ``chunking`` key exactly as a build's config
carries it; an empty ``{}`` config is valid, so only the chunking block can
raise here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from core.builds.config import BuildConfigError, load_build_config

_FIXTURE = Path(__file__).parent / "fixtures" / "chunking_block_validity.json"
_CORPUS = json.loads(_FIXTURE.read_text("utf-8"))


def _apply(base: dict[str, Any], case: dict[str, Any]) -> Any:
    if "replace" in case:
        return case["replace"]
    block = copy.deepcopy(base)
    for path, value in case.get("set", {}).items():
        block[path] = value
    for path in case.get("unset", []):
        del block[path]
    return block


@pytest.mark.parametrize("case", _CORPUS["cases"], ids=[c["name"] for c in _CORPUS["cases"]])
def test_server_verdict_matches_corpus(case: dict[str, Any]) -> None:
    block = _apply(_CORPUS["base"], case)
    if case["valid"]:
        load_build_config({"chunking": block})  # must not raise
    else:
        with pytest.raises(BuildConfigError):
            load_build_config({"chunking": block})
