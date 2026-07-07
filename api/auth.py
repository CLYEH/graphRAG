"""Auth placeholder (§23) — the SEAM, not the policy.

DESIGN §23: "swap the implementation, keep the scheme." The frozen contract
advertises ``bearerAuth`` (HTTP bearer); this module is the dependency every
future protected route depends on. It EXTRACTS the bearer credential and
returns a Principal, but does NOT yet validate it — real verification lands
when auth is wired, changing only this function's body, never its signature
or the OpenAPI scheme.

Deliberately permissive: a missing/empty token yields the anonymous
Principal rather than a 401. Rejecting here would need an auth error code,
and the frozen §27.2 enum has none — inventing one would breach DR-002. So
the placeholder never rejects; it only establishes where the token arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPBearer

#: auto_error=False: FastAPI must NOT raise its own non-envelope 403 on a
#: missing token — the placeholder decides (and today it admits anonymous).
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """Who is calling. ``token`` is the raw bearer credential (None when
    absent); ``is_authenticated`` stays False until real validation lands."""

    token: str | None
    is_authenticated: bool = False


async def current_principal(
    credentials: Annotated[object, Depends(_bearer)],
) -> Principal:
    """The auth dependency — extract the bearer token, defer validation.
    Swap this body to enforce; the signature and the ``bearerAuth`` scheme
    stay put."""
    token = getattr(credentials, "credentials", None)
    return Principal(token=token, is_authenticated=False)
