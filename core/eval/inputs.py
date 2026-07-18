"""The on-disk eval-input file layout (DR-010; CFG1 landed the unification —
the query policy now lives in the ``projects.config`` registry, so the only
FILE input left is the golden set). Defined ONCE here so the worker that
LOADS the golden set (``api.workers.build_worker.run_eval_task``) and the
idempotency fingerprint that HASHES it (``core.eval.idempotency``) can never
drift onto different files.
"""

from __future__ import annotations

from pathlib import Path


def eval_input_paths(root: Path) -> tuple[Path]:
    """The golden-set file path under a project's config ``root`` —
    ``<root>/eval/golden.yaml`` (a one-tuple: the policy component moved to
    the registry with CFG1, and keeping the tuple shape makes the change
    loud at every consumer). Both the run and its idempotency fingerprint
    resolve the file through here so they stay in lockstep."""
    return (root / "eval" / "golden.yaml",)
