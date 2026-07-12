"""Sources → connector dispatch (BA2c-2b) — a project's ``sources`` rows become
the §5-step-1 :class:`DocumentPayload` stream the ingest stage persists.

BA1a's ``sources`` table stores a free-form ``(kind, uri, metadata)`` per source;
the C2 connectors (:mod:`core.ingest.connectors`) each read one §2 source FAMILY
(free text vs. structured/tabular). This module is the routing layer between them:
it maps each :class:`~core.registry.store.Source` by ``kind`` to the right
connector call. ``kind`` is free-form in the store (``str | None``, no enum), so
this dispatch DEFINES the vocabulary it recognizes — the two connector families:

* ``"text"`` → :func:`~core.ingest.connectors.read_text_documents` over the
  directory the ``file://`` ``uri`` names (``.txt``/``.md``).
* ``"structured"`` → :func:`~core.ingest.connectors.read_csv_rows` over the CSV
  the ``file://`` ``uri`` names, with ``table`` and ``pk_column`` read from
  ``metadata`` (§27.2 row refs cite ``table + pk``).

Any other kind (``None``, ``url``, ``database``, a typo) fails loud: there is no
connector for it yet, and a build over an unroutable source must not silently
ingest zero documents. Only ``file://`` URIs are wired — a real registration
carries one (the connectors themselves emit ``Path.as_uri()``), and a bare
Windows path would mis-parse (a drive letter reads as a URI scheme), so the
scheme is required rather than guessed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from core.ingest.connectors import DocumentPayload, read_csv_rows, read_text_documents
from core.registry.store import Source

#: Source kinds this task wires to a C2 connector. The ``sources`` table/API
#: accept any kind string; a build over a kind absent from this tuple fails loud
#: (no connector) rather than ingesting nothing.
SUPPORTED_SOURCE_KINDS = ("text", "structured")


class SourceResolutionError(ValueError):
    """A registered source cannot be turned into a payload stream — an
    unsupported/missing ``kind``, a non-canonical ``file://`` uri (one whose
    displayed path is not what the worker would read), or ``structured``
    metadata missing ``table``/``pk_column``. Loud at ingest time, never a
    silent empty ingest."""


def _local_path(source: Source) -> Path:
    """The local filesystem path a ``file://`` source uri names — verbatim.

    Raises unless the DISPLAYED uri reads back to exactly the path the worker
    opens. ``urlsplit``/``url2pathname`` silently reinterpret a whole family of
    non-canonical forms — tab/newline stripped at any position, edge whitespace
    stripped, a host dropped (``file://nas/corpus`` reads ``/corpus``), query/
    fragment stripped, percent-decoding springing separators or dot segments the
    filesystem then resolves (``%2F..%2F`` → ``//../``), ``//``-leading paths
    read as UNC roots, an empty path as the worker's cwd. A build over any of
    those ingests a DIFFERENT tree than the registered uri appears to name —
    wrong data, strictly worse than a loud failure. The Console mirrors this
    gate client-side, but CLI/API/MCP-triggered builds reach here directly, so
    the source of truth enforces it (Codex #70 family).
    """
    uri = source.uri

    def _reject(why: str) -> SourceResolutionError:
        return SourceResolutionError(
            f"source {source.id} uri {uri!r} {why} — the worker would read a "
            "different path than the stored uri displays; register a canonical "
            "file:///absolute/path uri"
        )

    if uri != uri.strip():
        raise _reject("has leading/trailing whitespace (urlsplit strips it)")
    if any(ord(ch) < 0x20 for ch in uri):
        raise _reject("contains control characters (urlsplit strips tab/newline anywhere)")
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise SourceResolutionError(
            f"source {source.id} uri {source.uri!r} is not a file:// URI — only "
            f"file-backed sources are wired ({', '.join(SUPPORTED_SOURCE_KINDS)})"
        )
    if parsed.netloc:
        raise _reject(f"names a host {parsed.netloc!r} that url2pathname drops")
    if parsed.query or parsed.fragment:
        raise _reject("carries a query/fragment that urlparse strips from the path")
    decoded = unquote(parsed.path)
    if "\x00" in decoded:
        raise _reject("decodes to a path containing NUL, which no filesystem accepts")
    if "\\" in decoded:
        # on a Windows worker url2pathname treats "\" as a separator, so an
        # encoded "%2e%2e%5C" springs a "..\" traversal the "/"-segment checks
        # below can't see; on POSIX a literal backslash in a filename is exotic
        # at best — one canonical shape, so refuse it everywhere.
        raise _reject("decodes to a path containing backslashes (Windows separators)")
    if decoded in ("", "/"):
        raise _reject("names no path (the worker's cwd or the filesystem root)")
    if not decoded.startswith("/"):
        # file:../x or file:relative/x — a relative path resolves against the
        # WORKER's cwd, not anything the stored uri names; it would also break the
        # leading-slash assumption of the segment split below.
        raise _reject("names a relative path (resolved against the worker's cwd)")
    if decoded.startswith("//"):
        raise _reject("decodes to a //-leading path (reinterpreted as a UNC root)")
    segments = decoded.split("/")[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]  # one trailing slash: the idiomatic directory form
    if not segments or any(seg in ("", ".", "..") for seg in segments):
        raise _reject("contains empty or dot path segments (resolved away from the display)")
    # url2pathname handles the leading slash and Windows drive letters.
    return Path(url2pathname(parsed.path))


def _required_meta(source: Source, key: str) -> str:
    """A required non-empty string from a structured source's metadata."""
    value = source.metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SourceResolutionError(
            f"structured source {source.id} needs a non-empty string {key!r} in "
            f"metadata (read_csv_rows cites table + pk per §27.2)"
        )
    return value


def resolve_source(source: Source) -> Iterator[DocumentPayload]:
    """The §5-step-1 payload stream for one source, dispatched by ``kind``.

    Raises :class:`SourceResolutionError` eagerly for an unsupported/missing kind,
    a non-``file://`` uri, or missing structured metadata. The connector's own
    lazy failures (a missing directory, a CSV header without the pk column) still
    surface loud when the ingest stage iterates the stream.
    """
    if source.kind == "text":
        return read_text_documents(_local_path(source))
    if source.kind == "structured":
        return read_csv_rows(
            _local_path(source),
            table=_required_meta(source, "table"),
            pk_column=_required_meta(source, "pk_column"),
        )
    raise SourceResolutionError(
        f"source {source.id} has unsupported kind {source.kind!r} — wired kinds are "
        f"{', '.join(SUPPORTED_SOURCE_KINDS)} (url/database have no C2 connector yet)"
    )
