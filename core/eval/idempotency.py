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
    validates them loud). Each file's basename labels its bytes so two files can never
    alias."""
    root = safe_project_subdir(projects_dir, project)
    if root is None:
        return "unsafe-project"
    digest = hashlib.sha256()
    for path in eval_input_paths(root):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"")
        digest.update(b"\0")
    return digest.hexdigest()
