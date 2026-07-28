"""Filesystem path-safety helpers.

A project name reaches the filesystem as a PATH COMPONENT in more than one place
— the managed upload corpus (``api.routers.uploads``) and the on-disk eval config
(``api.workers.build_worker``) — but project names are only length-validated
(``ProjectCreate``). A name like ``..`` or one carrying separators would let the
resolved path ESCAPE the configured root, reading or writing outside it. This is
the ONE guard both call sites share, so the containment rule can't be fixed in one
place and forgotten in the sibling.


Also hosts :func:`is_path_addressable` — a REST-transport predicate rather
than a filesystem one, but the same question in a different medium ("can this
project key survive being a single path segment?") and derived the same way,
so the two rules live together instead of drifting apart.
"""

from __future__ import annotations

from pathlib import Path


def safe_project_subdir(base: Path, project: str) -> Path | None:
    """``base / project`` resolved, but ONLY when it is a DIRECT child of ``base``.

    Returns the resolved child directory, or ``None`` when ``project`` is not a
    safe single path component of ``base`` (e.g. ``..``, an absolute path, or a
    name with separators) — so the caller can map the refusal to its own error (a
    400 at the API boundary, a failed job in the worker). The check is pure path
    math plus a ``resolve()``; it does not create anything.

    ``project`` must be a single RELATIVE path component, validated BEFORE the
    resolve: ``resolve()`` collapses ``foo/../bar`` to ``bar`` and takes an absolute
    right operand verbatim, so either would pass the parent-equality check below
    while ALIASING a *different* project's dir (``base/bar``) rather than naming a
    child literally called ``foo/../bar``. ``PurePath.name`` normalizes ``.``/``..``
    to ``''`` and drops any parent/anchor, so ``project != Path(project).name``
    rejects separators (``/`` on any OS; ``\\`` too on Windows), absolute paths, and
    the dot names in one shot; the explicit ``\\`` check covers a backslash on a
    POSIX worker (where it is a legal filename char, not a separator, so it would
    otherwise slip through and later alias on a Windows worker sharing the store)."""
    if not project or "\\" in project or project != Path(project).name:
        return None
    resolved = (base / project).resolve()
    return resolved if resolved.parent == base.resolve() else None


def is_path_addressable(project: str) -> bool:
    """Whether a project key can ride in a REST path segment.

    QA10: the write path accepted names the read path cannot address, so a
    project could be created and then be unreachable — every subsequent
    ``GET/PATCH/DELETE /projects/{name}`` 404s, including the delete that would
    remove it.

    The set is COMPLETE and derived from the transport, not guessed (the same
    derivation the Console records in ``web/src/project/projectRoute.ts``):

    1. ``.`` and ``..`` — ``encodeURIComponent`` leaves them as bare dot
       tokens, which the URL parser normalizes away before routing (``.`` ->
       ``/projects/health``, ``..`` -> ``/health``); ``%2e`` normalizes too, so
       no encoding survives.
    2. any name containing ``/`` — encoded to ``%2F``, which the ASGI server
       decodes back to ``/`` before routing, so the single-segment
       ``{project}`` route (not ``{project:path}``) misses it.

    Every other character percent-encodes to a non-dot ``%XX`` that decodes to
    itself, so query/hash characters, spaces, unicode and ``%`` all round-trip.
    """
    return project not in (".", "..") and "/" not in project
