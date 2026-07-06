"""Why: golden.yaml GATES activation — a loader that under-validates lets a
typo'd expectation silently never run (false-green gate); one that
over-rejects blocks legitimate golden sets. The frozen schema is the gate;
failures must be loud and name where."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.eval.golden import GoldenError, load_golden


def _valid() -> dict:  # type: ignore[type-arg]
    return {
        "schema_version": "1.0",
        "cases": [
            {
                "question": "Who partners with Acme?",
                "mode": "semantic",
                "expects": {"must_contain_entities": ["Globex"]},
                "min_score": 0.5,
            }
        ],
    }


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "golden.yaml"
    if isinstance(document, str):
        path.write_text(document, "utf-8")
    else:
        path.write_text(yaml.safe_dump(document), "utf-8")
    return path


def test_a_valid_golden_set_loads_typed(tmp_path: Path) -> None:
    golden = load_golden(_write(tmp_path, _valid()))
    assert len(golden.cases) == 1
    case = golden.cases[0]
    assert case.mode == "semantic" and case.min_score == 0.5
    assert case.expects == {"must_contain_entities": ["Globex"]}


def test_missing_file_and_bad_yaml_fail_loud(tmp_path: Path) -> None:
    with pytest.raises(GoldenError, match="not found"):
        load_golden(tmp_path / "nope.yaml")
    with pytest.raises(GoldenError, match="not valid YAML"):
        load_golden(_write(tmp_path, ":\n  - ]["))


def test_contract_violations_name_where(tmp_path: Path) -> None:
    document = _valid()
    document["cases"][0]["expects"] = {}  # minProperties 1 — nothing to check
    with pytest.raises(GoldenError, match="expects"):
        load_golden(_write(tmp_path, document))

    document = _valid()
    document["cases"][0]["mode"] = "teleport"  # outside the frozen QueryMode enum
    with pytest.raises(GoldenError, match="mode"):
        load_golden(_write(tmp_path, document))

    document = _valid()
    document["cases"][0]["typo_field"] = True  # human-authored: unknown keys rejected
    with pytest.raises(GoldenError, match="typo_field"):
        load_golden(_write(tmp_path, document))


def test_missing_schema_names_every_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wheel-parity (the C8 lesson): with neither the repo-root nor the
    packaged contracts copy present, the loader fails LOUD naming both
    candidates — never a bare FileNotFoundError."""
    import core.eval.golden as module

    monkeypatch.setattr(module, "_SCHEMA_CANDIDATES", (tmp_path / "a.json", tmp_path / "b.json"))
    with pytest.raises(GoldenError, match="not found — looked in"):
        load_golden(_write(tmp_path, _valid()))
