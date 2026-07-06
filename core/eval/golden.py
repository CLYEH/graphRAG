"""Golden-set loading (§20/§27.5, DR-002; C10).

``eval/golden.yaml`` is human-authored input that GATES activation (§14
preflight), so it is validated against the FROZEN
``contracts/golden.schema.json`` before any value is trusted — a typo'd
expectation that silently never ran would turn the eval gate false-green.
Failures are loud and name the offending path (the query-policy loader's
convention; same two-candidate schema resolution so installed wheels find
the packaged contracts copy).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

#: source checkout keeps contracts/ at the repo root; an installed wheel
#: ships the build-time copy inside core/ (pyproject force-include).
_SCHEMA_CANDIDATES = (
    Path(__file__).resolve().parent.parent.parent / "contracts" / "golden.schema.json",
    Path(__file__).resolve().parent.parent / "contracts" / "golden.schema.json",
)


class GoldenError(ValueError):
    """The golden set is missing or violates the frozen contract — raised at
    eval startup (fail loud; a mis-authored gate must never run half-armed)."""


@dataclass(frozen=True)
class GoldenCase:
    """One golden question (§20): the question text is the case's stable
    identity across builds."""

    question: str
    mode: str
    expects: dict[str, Any]
    min_score: float


@dataclass(frozen=True)
class GoldenSet:
    cases: tuple[GoldenCase, ...] = field(default=())


def _schema_text() -> str:
    for candidate in _SCHEMA_CANDIDATES:
        if candidate.is_file():
            return candidate.read_text("utf-8")
    raise GoldenError(
        "golden.schema.json not found — looked in: " + ", ".join(str(c) for c in _SCHEMA_CANDIDATES)
    )


def load_golden(path: Path) -> GoldenSet:
    """Load + validate one project's ``eval/golden.yaml``.

    The frozen schema is the gate (DR-002): schema violations raise
    :class:`GoldenError` naming where; only then are typed cases built."""
    try:
        raw = yaml.safe_load(path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise GoldenError(f"golden set not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise GoldenError(f"golden set is not valid YAML: {exc}") from exc

    schema = json.loads(_schema_text())
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(part) for part in first.absolute_path) or "<root>"
        raise GoldenError(
            f"golden set violates the frozen contract at {where}: {first.message}"
            + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else "")
        )

    return GoldenSet(
        cases=tuple(
            GoldenCase(
                question=case["question"],
                mode=case["mode"],
                expects=dict(case["expects"]),
                min_score=float(case["min_score"]),
            )
            for case in raw["cases"]
        )
    )
