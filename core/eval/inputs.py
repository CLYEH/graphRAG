"""The on-disk eval-input file layout (DR-010; CFG1 will later unify this with the
``projects.config`` registry). Defined ONCE here so the worker that LOADS the golden
set + query policy (``api.workers.build_worker.run_eval_task``) and the idempotency
fingerprint that HASHES them (``core.eval.idempotency``) can never drift onto
different files — a mismatch would scope eval idempotency to content the run does
not actually read.
"""

from __future__ import annotations

from pathlib import Path


def eval_input_paths(root: Path) -> tuple[Path, Path]:
    """The ``(golden set, query policy)`` file paths under a project's config
    ``root`` — ``<root>/eval/golden.yaml`` and ``<root>/config.yaml``. The single
    source of the eval-input layout; both the run and its idempotency fingerprint
    resolve their files through here so they stay in lockstep."""
    return root / "eval" / "golden.yaml", root / "config.yaml"
