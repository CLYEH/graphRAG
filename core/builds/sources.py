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

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlparse
from urllib.request import url2pathname

from core.ingest.connectors import DocumentPayload, read_csv_rows, read_text_documents
from core.registry.store import Source

#: Source kinds this task wires to a C2 connector. The ``sources`` table/API
#: accept any kind string; a build over a kind absent from this tuple fails loud
#: (no connector) rather than ingesting nothing.
SUPPORTED_SOURCE_KINDS = ("text", "structured")

#: The one path segment a colon may appear in: a Windows drive ("C:"), which
#: ``Path.as_uri()`` emits and ``url2pathname`` resolves as displayed.
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")

#: A ``%`` not followed by two hex digits. ``unquote`` leaves it literal and never
#: raises, while the Console's ``decodeURIComponent`` throws — so the two gates
#: would disagree on it (a directory named ``100%``).
_MALFORMED_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


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
    if "%2f" in parsed.path.lower():
        # No filesystem permits "/" in a filename, so an encoded %2F can only be
        # an alternative spelling of a separator — one that hides the segment
        # boundary from the displayed uri. One canonical shape: separators are
        # literal "/".
        raise _reject("encodes the path separator (%2F) — separators must be literal")
    if "%3a" in parsed.path.lower():
        # url2pathname makes its STRUCTURAL decisions on the still-encoded path: it
        # detects the drive from a LITERAL ":". The checks below run on the decoded
        # path, so an encoded drive colon would satisfy them ("C:" in segment 0) while
        # the read silently drops out of the drive branch — "/C%3A/corpus" opens
        # "\C:\corpus" (no drive), not "C:\corpus". The drive separator must be literal
        # for the same reason "/" must be: the check and the read have to see the same
        # structure. (A colon outside the drive position is refused below regardless.)
        raise _reject("encodes the drive separator (%3A) — the drive colon must be literal")
    if _MALFORMED_ESCAPE.search(parsed.path):
        # Like the triple-slash rule below, this one reads what it displays — unquote
        # leaves a malformed escape LITERAL and never raises ("/data/100%" reads
        # \data\100%). The Console's decodeURIComponent throws, so accepting it here
        # would split enforcement the same way: a directory legitimately named "100%",
        # registered via API/CLI, builds from the CLI and makes the Console mark the
        # source unresolvable — blocking every build for the project. It also ALIASES:
        # "/data/100%" and the canonical "/data/100%25" read the same path, so two
        # displayed uris would name one file. Path.as_uri() emits %25, so the canonical
        # spelling stays registerable and nothing real is over-blocked.
        raise SourceResolutionError(
            f"source {source.id} uri {uri!r} contains a malformed percent-escape — it "
            "resolves to the path it displays (unquote leaves it literal), but the "
            "Console's decoder refuses this shape, so a build over it is runnable from "
            "the CLI and never from the UI; encode a literal '%' as '%25'"
        )
    try:
        unquote_to_bytes(parsed.path).decode("utf-8")
    except UnicodeDecodeError as exc:
        # The SECOND and last place the two decoders disagree (the first is the malformed
        # escape above). These escapes ARE two hex digits, so the check above passes them
        # — the disagreement is a layer down: unquote defaults to errors="replace", so it
        # never raises and silently swaps the undecodable bytes for U+FFFD ("/data/%FF"
        # reads "/data/�"), while decodeURIComponent throws. So this is both defects
        # at once — the worker opens a MANGLED path (display≠read, hence _reject), and
        # the Console refuses what the SoR accepts (split enforcement). Reachable without
        # an adversary: a POSIX filename is a byte string, so a file named with raw byte
        # 0xE9 has exactly this canonical as_uri().
        raise _reject(
            "contains percent-escapes that are not valid UTF-8 — unquote replaces the "
            "undecodable bytes with U+FFFD, so the worker opens a different path than "
            "the uri displays (and the Console's decoder refuses it outright)"
        ) from exc
    decoded = unquote(parsed.path)
    if "\x00" in decoded:
        raise _reject("decodes to a path containing NUL, which no filesystem accepts")
    if "\\" in decoded:
        # on a Windows worker url2pathname treats "\" as a separator, so an
        # encoded "%2e%2e%5C" springs a "..\" traversal the "/"-segment checks
        # below can't see; on POSIX a literal backslash in a filename is exotic
        # at best — one canonical shape, so refuse it everywhere.
        raise _reject("decodes to a path containing backslashes (Windows separators)")
    if "|" in decoded:
        # a pipe is the legacy spelling of the DRIVE separator — url2pathname's first
        # act is url.replace(":", "|"), so the two are the same character to it, and a
        # pipe anywhere makes the preceding letter a drive ("/a|/corpus" → "A:\corpus").
        # Windows reserves "|" in filenames outright, so refusing it everywhere costs
        # nothing — same trade as the backslash above.
        raise _reject("contains a pipe — the Windows drive separator ('a|' reads as 'a:')")
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
    for index, seg in enumerate(segments):
        if ":" in seg and not (index == 0 and _WINDOWS_DRIVE.fullmatch(seg)):
            # The colon IS the drive separator to url2pathname (it maps ":" → "|" and
            # takes the letter before the FIRST one as the drive), so a colon in any
            # other position silently re-roots the path: "/data/foo:bar" opens "O:bar",
            # "/data:x/y" opens "A:x\y". Two forms ("/C:/data/foo:bar", "/1:/data") even
            # escape as a raw OSError. Constrain the colon to the drive position rather
            # than refusing it outright: "file:///C:/…" is the canonical Windows drive
            # form (Path.as_uri() emits it) and must stay registerable. A POSIX file
            # named "foo:bar" becomes unregisterable — the same trade as "\" and "|",
            # and the right one: nothing here knows the worker's OS, and the
            # alternative is silently opening a different volume.
            raise _reject(
                f"has a colon in segment {seg!r}, outside the Windows drive position "
                "(url2pathname reads every ':' as the drive separator)"
            )
    if len(segments) == 1 and _WINDOWS_DRIVE.fullmatch(segments[0]) and not decoded.endswith("/"):
        # "file:///C:" displays as the C: drive but resolves to the DRIVE-RELATIVE
        # Path("C:") — is_absolute() is False, so the worker reads its current directory
        # on that drive. This is the Windows drive spelling of the cwd hazard the
        # empty-path check above already rejects, and the colon rule is what blesses a
        # bare "C:" segment, so it's this rule's to close. "file:///C:/" (the drive root)
        # resolves absolute and stays accepted.
        raise _reject(
            "names a bare drive with no trailing slash — url2pathname yields the "
            "DRIVE-RELATIVE 'C:' (the worker's current directory on that drive), not "
            "the drive root; register 'file:///C:/'"
        )
    if not uri.lower().startswith("file:///"):
        # Everything above rejects a uri whose READ diverges from its display. This one
        # doesn't: "file:/data/corpus" resolves to exactly the path it shows (urlparse
        # yields the same absolute path as the triple-slash form). What it splits is
        # ENFORCEMENT — the Console gate requires the triple-slash form, so a source
        # registered via API/CLI in this shape builds fine there while the Console
        # marks it unresolvable and refuses to run any build for the project. One
        # canonical shape means one accept set on both sides of the API, so the SoR
        # refuses the shape too. (Not routed through _reject: its "the worker would
        # read a different path" wording would be false here.)
        raise SourceResolutionError(
            f"source {source.id} uri {uri!r} is not the canonical triple-slash form "
            "file:///absolute/path — it resolves to the same path, but the Console "
            "gate refuses this shape, so a build over it is runnable from the CLI and "
            "never from the UI; register the triple-slash form"
        )
    # Everything above validates the uri's SHAPE. This last check validates the actual
    # RESULT, and it is the only rule here that depends on the worker's own OS: on a
    # Windows worker "/data/corpus" resolves to "\data\corpus" — rooted, but on whatever
    # drive the process currently happens to be using, so one stored uri reads a
    # different tree depending on the worker's cwd. Same current-directory dependence as
    # the bare drive above, in the shape that otherwise looks canonical. On a POSIX
    # worker (the deployment) every accepted form is already absolute, so this never
    # fires there and changes nothing.
    #
    # The Console cannot mirror this one — a browser cannot know the worker's OS — so it
    # is the single place the two accept sets legitimately differ. It differs in the SAFE
    # direction: a loud build failure naming the uri, never a silent read of the wrong
    # tree. (tests/fixtures/canonical_file_uri.json marks which accepts are POSIX-only.)
    resolved = Path(url2pathname(parsed.path))
    if not resolved.is_absolute():
        raise _reject(
            f"resolves to {str(resolved)!r}, which is not absolute on this worker — it "
            "is rooted on whichever drive the process is currently using, so the tree it "
            "reads depends on the worker's cwd; name the drive (file:///C:/data/corpus)"
        )
    return resolved


def _files_metadata(source: Source) -> dict[str, dict[str, Any]] | None:
    """The per-file metadata envelopes an upload stashed on a managed text source
    (``metadata["files"]``, keyed by stored filename — see
    :func:`core.registry.store.upsert_managed_source`), or None for a source with
    no such stash (a non-upload text source, scanned as a plain directory).

    A ``files`` key that IS a dict marks the source MANAGED (its registered file
    list is authoritative — see :func:`~core.ingest.connectors.read_text_documents`).
    A malformed entry (a value that is not an envelope object) is rejected LOUD,
    never silently dropped: dropping every bad entry would leave ``{}``, which the
    connector would read as a plain directory and scan — ingesting unregistered
    orphan files the managed list was supposed to exclude. Threaded into the text
    connector so each document carries its DR-010 envelope onto ``documents.metadata``.
    """
    files = source.metadata.get("files")
    if not isinstance(files, dict):
        return None
    validated: dict[str, dict[str, Any]] = {}
    for name, env in files.items():
        if not isinstance(env, dict):
            raise SourceResolutionError(
                f"managed source {source.id} file {name!r} has a non-object metadata "
                f"entry {type(env).__name__} — the managed file list maps each stored "
                "name to its DR-010 envelope object; a malformed entry fails loud rather "
                "than silently degrading the source to an unmanaged directory scan"
            )
        validated[name] = env
    return validated


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
        return read_text_documents(_local_path(source), _files_metadata(source))
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
