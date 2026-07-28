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
    child literally called ``foo/../bar``. That component rule now lives in
    :func:`is_safe_path_component` — see there for what it refuses and why; this
    function adds only the resolve/parent-equality check on top of it."""
    if not is_safe_path_component(project):
        return None
    resolved = (base / project).resolve()
    return resolved if resolved.parent == base.resolve() else None


def is_safe_path_component(name: str) -> bool:
    """Whether ``name`` is a single, relative, non-aliasing path component.

    Extracted from :func:`safe_project_subdir` so the WRITE boundary and the
    filesystem guard share ONE definition instead of two that can drift — the
    drift is not hypothetical: QA10's first round validated REST addressability
    and left this rule unshared, so ``a\\b``, ``a:b`` and ``C:evil`` were
    accepted at creation and then rejected by every filesystem-backed feature
    (uploads 400, eval preflight failure) for a project that already existed.

    ``PurePath.name`` drops any parent/anchor, so ``name != Path(name).name``
    rejects separators (``/`` on any OS; ``\\`` too on Windows) and absolute or
    drive-relative paths. The explicit ``\\`` test additionally covers a
    backslash on a POSIX worker, where it is a legal filename character rather
    than a separator — so it would otherwise pass here and later ALIAS on a
    Windows worker sharing the same store.

    The dot names are refused EXPLICITLY rather than by that normalization: on
    Python 3.13 only ``"."`` collapses — ``Path("..").name == ".."`` (measured),
    so the older prose claiming both were caught "in one shot" was wrong about
    ``..``. ``safe_project_subdir`` never had a hole (its resolve/parent-equality
    check refuses it downstream), but this predicate is public now, and the
    first caller to use it alone would have inherited one.

    Answers CONTAINMENT ONLY — "can this name be a safe child of a base
    directory" — and deliberately nothing about whether the name is a wise one
    to accept. That distinction is load-bearing: this predicate also gates the
    CLEANUP path (``safe_project_subdir`` → ``_detach_upload_dir``), so a rule
    added here retroactively strands data belonging to projects that ALREADY
    EXIST — the delete endpoint would commit the registry row's removal while
    skipping a directory it now refuses to name, orphaning the files. The
    trailing-space/dot aliasing rule lives in :func:`is_creatable_project_key`
    for exactly that reason (Codex #149 r3).
    """
    return bool(name) and name not in (".", "..") and "\\" not in name and name == Path(name).name


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


def is_creatable_project_key(project: str) -> bool:
    """Whether a NEW project may be created under this key (pure-string half).

    A key has to survive several transports, and QA10 found them one at a time
    rather than by enumeration — which is the lesson, not the list. The two
    answerable from the string alone live here: the REST path segment
    (:func:`is_path_addressable`) and the filesystem component
    (:func:`is_safe_path_component`, the rule :func:`safe_project_subdir`
    enforces). Failing either creates a project that EXISTS but cannot be used
    — unreachable over REST, or reachable but unable to accept uploads.

    There is a THIRD surface, and it is deliberately NOT here: the canonical
    corpus ``file://`` uri (``a|b`` is a fine path component whose ``as_uri()``
    encodes to a form no build can resolve). It needs settings and a filesystem
    ``resolve()``, so it is asked by ``api.routers._corpus.reject_unsafe_corpus_path``
    at the write boundary instead.

    So: a new surface belongs HERE if it is decidable from the key alone, and
    in that helper if it needs configuration or I/O. Putting an I/O rule here
    would make this predicate unusable in the Pydantic validator that is its
    whole point.

    CREATION-only, and the name says so (Codex #149 r3). The trailing-space/dot
    clause below is an ALIASING rule, not a containment one: Windows strips
    them at ``mkdir``, so ``p``, ``p`` + space and ``p.`` would land in one
    corpus directory (measured) — one project's uploads becoming another's
    documents. Refusing that for a key nobody holds yet costs nothing; putting
    it in the containment guard would retroactively make EXISTING POSIX
    projects with such names un-cleanable, because ``safe_project_subdir``
    would stop naming their directory and the delete endpoint would commit the
    row's removal while skipping the files. A guard that gates cleanup can only
    be tightened for data that does not exist yet.
    """
    return (
        is_path_addressable(project)
        and is_safe_path_component(project)
        and project == project.rstrip(" .")
        and _fits_a_path_component(project)
    )


#: The POSIX/NTFS single-component limit. Not a policy number — the filesystems
#: enforce it, and this task deliberately declined to invent a length cap.
_MAX_COMPONENT = 255


def _fits_a_path_component(name: str) -> bool:
    """Whether ``name`` fits in one directory entry on EVERY backing store.

    Codex #149 r4: a name longer than the component limit passes every other
    predicate here — and passes ``resolve()`` and the corpus-URI probe, since
    neither creates anything — so the project was persisted and the FIRST
    upload died in ``mkdir`` with ``ENAMETOOLONG``/``EINVAL``. Creatable and
    permanently unusable, which is this task's recurring shape.

    The UTF-8 BYTE count is the only check needed, and it is the strictest of
    the three units in play: bytes >= UTF-16 units >= code points, for every
    string. So bounding bytes bounds the others, and a separate ``len(name)``
    clause could never bind — stating it as "both limits, stricter wins" would
    describe a comparison the code does not make.

    Bytes rather than the platform's own unit because the limits DISAGREE and a
    store can be shared between workers on different platforms (measured): NTFS
    counts 255 UTF-16 units, so 200 CJK characters succeed on Windows; ext4
    counts 255 BYTES, where the same name is 600 and fails. Refusing on
    every platform what breaks on any is the rule the backslash clause already
    follows, for the same shared-store reason.

    An earlier round of this task dismissed length as "an invented policy the
    frozen contract does not set", and that was right about an ARBITRARY cap
    and wrong here: this bound is measured off the backing store, exactly like
    the separator and drive-prefix rules beside it.

    Creation-only (it lives in :func:`is_creatable_project_key`, not the
    containment guard) so a legacy over-long directory — reachable on a store
    whose limit is higher — stays cleanable.
    """
    return len(name.encode("utf-8")) <= _MAX_COMPONENT
