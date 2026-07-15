"""Filesystem path-safety helpers.

A project name reaches the filesystem as a PATH COMPONENT in more than one place
— the managed upload corpus (``api.routers.uploads``) and the on-disk eval config
(``api.workers.build_worker``) — but project names are only length-validated
(``ProjectCreate``). A name like ``..`` or one carrying separators would let the
resolved path ESCAPE the configured root, reading or writing outside it. This is
the ONE guard both call sites share, so the containment rule can't be fixed in one
place and forgotten in the sibling.
"""

from __future__ import annotations

from pathlib import Path


def safe_project_subdir(base: Path, project: str) -> Path | None:
    """``base / project`` resolved, but ONLY when it is a DIRECT child of ``base``.

    Returns the resolved child directory, or ``None`` when ``project`` is not a
    safe single path component of ``base`` (e.g. ``..``, an absolute path, or a
    name with separators) — so the caller can map the refusal to its own error (a
    400 at the API boundary, a failed job in the worker). The check is pure path
    math plus a ``resolve()``; it does not create anything."""
    resolved = (base / project).resolve()
    return resolved if resolved.parent == base.resolve() else None
