"""Why: ``resolved_advertised_host`` is the ONE resolver both the MCP-info
endpoint and the ``serve-mcp`` CLI warning speak through (class 5) — and its
reject branches are the contract-integrity guarantee itself: every value no
valid URI can carry must raise, or the endpoint would answer 200 with a
``url`` violating the frozen ``McpInfo.url`` ``format: uri``. The accept side
is pinned by the api-level matrix in test_health_routers; this file pins the
attack/reject side and the CLI-path (``reached_host=None``) semantics —
guards.md's dual, both directions.
"""

from __future__ import annotations

import pytest

from core.mcp.addressing import resolved_advertised_host


class _Settings:
    """Typed stub satisfying the McpAddressSettings protocol structurally."""

    def __init__(self, bind: str = "127.0.0.1", public: str | None = None) -> None:
        self.mcp_http_host = bind
        self.mcp_public_host = public


def _settings(bind: str = "127.0.0.1", public: str | None = None) -> _Settings:
    return _Settings(bind, public)


@pytest.mark.parametrize(
    "bad",
    [
        "mcp.example:8443",  # embedded port would corrupt host:port
        "bad host",  # whitespace — no URI can carry it
        "fe80::1%eth0",  # scoped IPv6, bare (Python parses scope ids!)
        "[fe80::1%eth0]",  # scoped IPv6, bracketed — RFC 9844 reverted 6874
        "[foo]",  # brackets around a non-IPv6 value
    ],
)
def test_unusable_values_raise_instead_of_corrupting_the_url(bad: str) -> None:
    with pytest.raises(ValueError):
        resolved_advertised_host(_settings(public=bad), reached_host="console.lan")


def test_an_empty_public_host_means_unset_not_invalid() -> None:
    # env vars are routinely set-empty; "" must behave like None (fall back to
    # the bind), not like a malformed value
    assert resolved_advertised_host(_settings(public=""), reached_host="x") == "127.0.0.1"


def test_the_reject_applies_to_the_bind_too_when_no_public_host_is_set() -> None:
    # the same guarantee must hold when the BIND is the source of the value
    with pytest.raises(ValueError):
        resolved_advertised_host(_settings(bind="lan host"), reached_host="console.lan")


def test_an_unspecified_reached_host_is_rejected_not_advertised() -> None:
    # nonsensical substitute input: advertising it would be the original P1
    with pytest.raises(ValueError):
        resolved_advertised_host(_settings(bind="0.0.0.0"), reached_host="0.0.0.0")


@pytest.mark.parametrize(
    ("bind", "public", "expected"),
    [
        ("0.0.0.0", None, None),  # CLI path: wildcard is request-dependent
        ("::", None, None),
        ("10.0.0.7", None, "10.0.0.7"),
        ("0.0.0.0", "mcp.lan", "mcp.lan"),  # the operator's answer resolves it
        ("::1", None, "[::1]"),
        ("0.0.0.0", "[::1]", "[::1]"),  # bracketing is idempotent
    ],
)
def test_cli_path_reports_request_dependence_as_none(
    bind: str, public: str | None, expected: str | None
) -> None:
    # reached_host=None is the CLI's call shape: it has no request, so a
    # wildcard resolves to None ("describe the fallback") rather than a guess
    assert resolved_advertised_host(_settings(bind, public)) == expected
