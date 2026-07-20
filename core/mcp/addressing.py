"""Where the DR-012 gateway is dialable from OUTSIDE — one resolver.

Shared by the API's MCP-info endpoint (``GET /projects/{project}/mcp``) and
the ``serve-mcp`` CLI's divergence warning, so the two can never disagree
about what the Console advertises (class 5 — one predicate, every consumer).

Decisions are made on the ADDRESS's own properties, never its spelling:
``ipaddress`` answers "is this the unspecified address?" for every spelling
of it and "is this IPv6?" for bracketing (Codex #113 P1 + gate-2's two
reproduced faces of the spelling-list version).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Protocol

#: RFC 3986 reg-name subset that covers real hostnames. Deliberately EXCLUDES
#: ``:`` (an embedded port or unbracketed IPv6 would corrupt the authority),
#: whitespace, and ``%`` (zone ids / percent-escapes) — a configured value
#: outside this set cannot appear in a valid ``http://host:port`` authority,
#: and advertising it anyway would violate McpInfo's ``format: uri``
#: (local batch review P2: ``mcp.example:8443`` / ``bad host``).
_REG_NAME = re.compile(r"^[A-Za-z0-9._~-]+$")


class McpAddressSettings(Protocol):
    """The two settings this resolver reads (structural — the CLI passes the
    real ``Settings``, tests pass a stub)."""

    mcp_http_host: str
    mcp_public_host: str | None


def resolved_advertised_host(
    settings: McpAddressSettings, reached_host: str | None = None
) -> str | None:
    """The URL-authority host an external agent should dial.

    Preference order: the explicit ``mcp_public_host`` (the operator's own
    answer), else the bind host when it names something dialable, else — for
    an unspecified ("every interface") bind — ``reached_host`` (the API passes
    the host the Console itself was reached on; the CLI passes None and gets
    ``None`` back, meaning "request-dependent — describe the fallback").

    Raises ``ValueError`` when the configured value cannot appear in a valid
    URI authority: a host carrying a port, whitespace, or a scoped IPv6
    literal — RFC 9844 reverted RFC 6874's scoped-address URI syntax, so
    ``fe80::1%eth0`` has no valid URL spelling at all. Callers fail LOUD
    instead of advertising a contract-invalid URL.

    IPv6 literals come back bracketed (idempotently — a bracketed input is
    unwrapped, validated, and re-wrapped), so ``host:port`` stays unambiguous.
    """
    host = settings.mcp_public_host or settings.mcp_http_host
    authority = _validate(host)
    if authority is not None:
        return authority
    # unspecified bind: substitute the reached host, or report request-dependence
    if reached_host is None:
        return None
    substituted = _validate(reached_host)
    if substituted is None:
        # the reached host itself being unspecified is nonsensical input;
        # treat it like any other unusable value
        raise ValueError(f"reached host is the unspecified address: {reached_host!r}")
    return substituted


def _validate(host: str) -> str | None:
    """``host`` as a URL authority, ``None`` for the unspecified address, or
    ``ValueError`` for a value no valid URI can carry."""
    bracketed = host.startswith("[") and host.endswith("]")
    bare = host[1:-1] if bracketed else host
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        if bracketed:
            raise ValueError(f"brackets around a non-IPv6 value: {host!r}") from None
        if not _REG_NAME.match(host):
            raise ValueError(
                f"not a URL-authority host (no ports, spaces, or zone ids): {host!r}"
            ) from None
        return host  # a hostname
    if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id is not None:
        raise ValueError(f"scoped IPv6 literal has no valid URI form (RFC 9844): {host!r}")
    if ip.is_unspecified:
        return None
    return f"[{bare}]" if isinstance(ip, ipaddress.IPv6Address) else bare
