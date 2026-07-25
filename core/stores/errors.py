"""The backing stores' client exception families, and their store names.

One home for two facts every degradation path needs (MCP2):

* **Which exceptions are store trouble.** Deliberately NOT ``Exception`` —
  an in-code bug must propagate loud; degradation is for store trouble only
  (the ``core/mcp/server._bounded`` doctrine).
* **Which store an exception belongs to.** A warning that says only
  ``store unavailable (ResponseHandlingException)`` is client-library jargon:
  with Qdrant down ``graph_query`` still works (measured: 87 results), with
  Postgres down every tool is dead — naming the store turns "give up" into
  "route around". Both the single-mode tools and hybrid's per-mode guard
  consume this map, so the two surfaces can never drift apart.
"""

from __future__ import annotations

# asyncpg ships no py.typed marker — the targeted ignore keeps strict mypy on
# for everything else (never a config-level loosening)
from asyncpg.exceptions import (  # type: ignore[import-untyped]
    InterfaceError,
    InternalClientError,
)
from neo4j.exceptions import DriverError, Neo4jError
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.exc import DBAPIError

#: The Postgres stack's client-side families BEYOND DBAPIError (MCP12): a
#: DEAD Postgres at connect time surfaces the raw builtin
#: ``ConnectionRefusedError`` (measured — SQLAlchemy's asyncpg dialect does
#: not wrap connect-time socket errors), and a connection LOST mid-call
#: raises asyncpg's own ``InternalClientError`` ("unexpected
#: connection_lost() call") / ``InterfaceError`` — none of them DBAPIError
#: subclasses, so a Postgres outage escaped §22 entirely and every MCP tool
#: returned a raw isError string. Raw socket errors reach us UNWRAPPED only
#: from the asyncpg stack (qdrant wraps its transport into ApiException /
#: ResponseHandlingException, neo4j into DriverError), so attributing the
#: builtin ConnectionError family to postgres is honest in this stack.
_POSTGRES_CLIENT_ERRORS: tuple[type[BaseException], ...] = (
    DBAPIError,
    ConnectionError,
    InterfaceError,
    InternalClientError,
)

#: driver-level trouble from Postgres (see ``_POSTGRES_CLIENT_ERRORS``),
#: Qdrant (ApiException covers HTTP errors and connection handling), and
#: Neo4j (Neo4jError = server, DriverError = connectivity).
STORE_CLIENT_ERRORS: tuple[type[BaseException], ...] = (
    *_POSTGRES_CLIENT_ERRORS,
    ApiException,
    Neo4jError,
    DriverError,
)


def store_name(exc: BaseException) -> str:
    """The backing store an exception family belongs to."""
    if isinstance(exc, _POSTGRES_CLIENT_ERRORS):
        return "postgres"
    if isinstance(exc, ApiException):
        return "qdrant"
    if isinstance(exc, (Neo4jError, DriverError)):
        return "neo4j"
    return "unknown store"
