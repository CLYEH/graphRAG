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
import json
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


#: label for the registry-sourced policy component inside the fingerprint —
#: NOT a filename: CFG1 moved the policy out of config.yaml, and the label
#: change (config.yaml → this) deliberately flips every pre-CFG1 fingerprint
#: (the inputs' SOURCE changed, so replaying a pre-migration key must not
#: hit a post-migration run)
POLICY_COMPONENT_LABEL = "registry:query_policy"


def policy_fingerprint_bytes(config: object) -> bytes:
    """The canonical bytes of a registry config's ``query_policy`` block —
    the ONE serialization the accept-time fingerprint and the worker's drift
    check both hash (CFG1: the registry is the policy SoR; file bytes are
    gone). Canonical = sorted keys, tight separators, UTF-8 — dict ordering
    or whitespace can never flip the digest. A missing/non-mapping block
    contributes empty bytes: the fingerprint stays stable and distinct (the
    eval job itself refuses to RUN without a valid policy, loud)."""
    if not isinstance(config, dict) or not isinstance(config.get("query_policy"), dict):
        return b""
    return json.dumps(
        config["query_policy"], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def read_and_fingerprint_eval_inputs(root: Path, policy_bytes: bytes) -> tuple[str, bytes]:
    """Read the golden set EXACTLY ONCE, join the caller's registry-policy bytes,
    and return ``(fingerprint, golden_bytes)`` — the worker's drift check and its
    parse score the SAME content. The golden set stays a FILE (``eval/golden.yaml``);
    the policy component is the caller's SINGLE registry read serialized by
    :func:`policy_fingerprint_bytes` (CFG1) — passing bytes in keeps this module
    connection-free and makes re-read TOCTOU structurally impossible on both
    components. A missing/unreadable golden file raises ``OSError``, which the
    worker preflight terminalizes as a failed job. Digest format =
    ``_fingerprint_of_reads``, identical to the accept-time path."""
    golden_path = eval_input_paths(root)[0]
    golden_bytes = golden_path.read_bytes()
    fingerprint = _fingerprint_of_reads(
        [(golden_path.name, golden_bytes), (POLICY_COMPONENT_LABEL, policy_bytes)]
    )
    return fingerprint, golden_bytes


def eval_inputs_fingerprint(projects_dir: Path, project: str, policy_bytes: bytes) -> str:
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
    for path in eval_input_paths(root):  # golden set only (policy = registry, CFG1)
        try:
            # Both filesystem calls are inside the guard: is_file() re-raises on a
            # non-searchable dir, read_bytes() on an unreadable file — either would
            # otherwise propagate as a 500 (see docstring). Missing → empty bytes.
            data = path.read_bytes() if path.is_file() else b""
        except OSError:
            data = b"\0unreadable\0"  # stable sentinel; the job is the sole loud path
        reads.append((path.name, data))
    reads.append((POLICY_COMPONENT_LABEL, policy_bytes))
    return _fingerprint_of_reads(reads)
