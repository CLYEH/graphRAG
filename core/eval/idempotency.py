"""Eval idempotency fingerprint (DR-010 / contract v1.2).

The async eval endpoint (``POST /projects/{project}/builds/{id}/eval``) is
idempotent per **(build, golden-set fingerprint)**: a duplicate run over the SAME
build and golden set replays via the ``Idempotency-Key``, but changing the golden
set or query policy within the key's TTL must NOT replay a run scored against the
stale inputs. The build is already named in the request path; this module supplies
the golden-set-fingerprint half so the request hash changes when the inputs do.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.eval.inputs import eval_input_paths
from core.paths import safe_project_subdir


def eval_inputs_fingerprint(projects_dir: Path, project: str) -> str:
    """A stable content fingerprint of a project's eval inputs (golden set + query
    policy). Hashes the raw file bytes of the SAME files the run reads
    (``eval_input_paths`` — the single layout definition both share), so ANY content
    change flips it and a reused ``Idempotency-Key`` no longer replays a run scored
    against the old inputs. A missing file contributes empty bytes (adding or removing
    one still changes the hash); an unsafe project name folds to a stable sentinel —
    the eval job refuses those the same way, and the fingerprint only needs to be
    stable and distinct per input set, not to prove the inputs are valid (the job
    validates them loud). A present-but-unreadable input contributes a stable sentinel
    rather than raising — whether the read itself fails (bad perms on the file, or a
    file that vanished after ``is_file`` — a TOCTOU) OR the ``is_file`` stat fails (a
    non-searchable parent dir raises ``PermissionError``, which ``is_file`` re-raises;
    only not-found is swallowed). This fingerprint is computed in the API BEFORE the
    eval job exists, so propagating either ``OSError`` would 500 the request and create
    NO watchable job, bypassing the worker preflight that terminalizes eval-input errors
    as a failed job (build_worker.run_eval_task). Each file's basename labels its bytes
    so two files can never alias."""
    root = safe_project_subdir(projects_dir, project)
    if root is None:
        return "unsafe-project"
    digest = hashlib.sha256()
    for path in eval_input_paths(root):
        digest.update(path.name.encode())
        digest.update(b"\0")
        try:
            # Both filesystem calls are inside the guard: is_file() re-raises on a
            # non-searchable dir, read_bytes() on an unreadable file — either would
            # otherwise propagate as a 500 (see docstring). Missing → empty bytes.
            data = path.read_bytes() if path.is_file() else b""
        except OSError:
            data = b"\0unreadable\0"  # stable sentinel; the job is the sole loud path
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()
