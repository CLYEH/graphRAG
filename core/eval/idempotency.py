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
from collections.abc import Iterable
from pathlib import Path

from core.eval.inputs import eval_input_paths
from core.paths import safe_project_subdir


def _fingerprint_of_reads(reads: Iterable[tuple[str, bytes]]) -> str:
    """The joint fingerprint of ``(basename, raw bytes)`` reads, in order. Each file's
    basename labels its bytes (bracketed by NULs) so two files can never alias. The one
    hashing definition, shared by ``eval_inputs_fingerprint`` (the API accept-time
    fingerprint over a tolerant read) and ``read_and_fingerprint_eval_inputs`` (the
    worker's single read), so both produce the SAME digest for the same file contents."""
    digest = hashlib.sha256()
    for name, data in reads:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def read_and_fingerprint_eval_inputs(root: Path) -> tuple[str, bytes, bytes]:
    """Read a project's eval inputs (golden set + query policy) EXACTLY ONCE and return
    ``(fingerprint, golden_bytes, policy_bytes)``, so the worker's drift check and its
    parse score the SAME bytes. A separate re-read for parsing (``load_golden`` /
    ``load_query_policy`` reading the paths again) would reopen a TOCTOU: an edit landing
    between the fingerprint and the parse would be scored under the stale accept-time
    fingerprint, defeating the drift guard. Reads with ``read_bytes`` (raw — matching the
    accept-time fingerprint's read exactly), so a present, readable input's digest equals
    the pinned one; a missing/unreadable input raises ``OSError`` here, which the worker
    preflight terminalizes as a failed job (an eval can't run without its inputs anyway).
    The digest format is ``_fingerprint_of_reads``, identical to the accept-time path."""
    golden_path, policy_path = eval_input_paths(root)
    golden_bytes = golden_path.read_bytes()
    policy_bytes = policy_path.read_bytes()
    fingerprint = _fingerprint_of_reads(
        [(golden_path.name, golden_bytes), (policy_path.name, policy_bytes)]
    )
    return fingerprint, golden_bytes, policy_bytes


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
    reads: list[tuple[str, bytes]] = []
    for path in eval_input_paths(root):
        try:
            # Both filesystem calls are inside the guard: is_file() re-raises on a
            # non-searchable dir, read_bytes() on an unreadable file — either would
            # otherwise propagate as a 500 (see docstring). Missing → empty bytes.
            data = path.read_bytes() if path.is_file() else b""
        except OSError:
            data = b"\0unreadable\0"  # stable sentinel; the job is the sole loud path
        reads.append((path.name, data))
    return _fingerprint_of_reads(reads)
