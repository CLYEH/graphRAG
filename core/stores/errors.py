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

from neo4j.exceptions import DriverError, Neo4jError
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.exc import DBAPIError

#: driver-level trouble from Postgres (DBAPIError), Qdrant (ApiException covers
#: HTTP errors and connection handling), and Neo4j (Neo4jError = server,
#: DriverError = connectivity).
STORE_CLIENT_ERRORS: tuple[type[BaseException], ...] = (
    DBAPIError,
    ApiException,
    Neo4jError,
    DriverError,
)


def store_name(exc: BaseException) -> str:
    """The backing store an exception family belongs to."""
    if isinstance(exc, DBAPIError):
        return "postgres"
    if isinstance(exc, ApiException):
        return "qdrant"
    if isinstance(exc, (Neo4jError, DriverError)):
        return "neo4j"
    return "unknown store"
