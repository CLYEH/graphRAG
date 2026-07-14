"""Why: the Console settings page decides whether an existing query_policy is a
usable spread base with a CLIENT-side predicate (web/src/api/queries.ts
isValidPolicyBlock), while the authority on validity is this server's
query_policy_from_mapping (frozen jsonschema + the §21 typed re-checks). When
the two drift, a hand-written policy the server rejects becomes a spread base
the settings PATCH "succeeds" with — and every query keeps 400ing after the UI
reported success (Codex #79 R2/R3: the silent brick).

So parity is enforced mechanically, from one corpus both suites read
(tests/fixtures/query_policy_validity.json — the vitest half is
web/src/api/policyValidityParity.test.ts; the canonical-file-uri corpus set
the pattern). This half runs the REAL validator, which also proves the
corpus's base — byte-equal to the Console's DEFAULT_QUERY_POLICY, asserted by
the vitest half — is accepted by the server the Console will hand it to.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from core.mcp.policy import PolicyError, query_policy_from_mapping

_FIXTURE = Path(__file__).parent / "fixtures" / "query_policy_validity.json"
_CORPUS = json.loads(_FIXTURE.read_text("utf-8"))


def _apply(base: dict[str, Any], case: dict[str, Any]) -> Any:
    if "replace" in case:
        return case["replace"]
    doc = copy.deepcopy(base)
    for path, value in case.get("set", {}).items():
        keys = path.split(".")
        cur = doc
        for k in keys[:-1]:
            cur = cur[k]
        cur[keys[-1]] = value
    for path in case.get("unset", []):
        keys = path.split(".")
        cur = doc
        for k in keys[:-1]:
            cur = cur[k]
        del cur[keys[-1]]
    return doc


@pytest.mark.parametrize("case", _CORPUS["cases"], ids=[c["name"] for c in _CORPUS["cases"]])
def test_server_verdict_matches_corpus(case: dict[str, Any]) -> None:
    document = _apply(_CORPUS["base"], case)
    if case["valid"]:
        query_policy_from_mapping(document)  # must not raise
    else:
        with pytest.raises(PolicyError):
            query_policy_from_mapping(document)
