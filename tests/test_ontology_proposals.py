"""Why: the proposal pool is the §6 待審池 — the ONLY thing standing between a
hallucinated LLM type and silent vocabulary growth. Its identity must be the
stable DR-007 fingerprint (so a rejected type never re-opens review across
builds), its decision fields must be present IFF decided (§17), and the 🔧
proposal_policy vocabulary must fail loudly on typos — a policy that silently
behaved like 'review' would make auto-adoption configuration a no-op.
"""

from __future__ import annotations

import re

import pytest
import sqlalchemy as sa

from core.graph.documents import TypeProposal
from core.graph.proposals import PROPOSAL_POLICIES, persist_proposals
from core.resolve.fingerprints import FINGERPRINT_VERSION, proposal_key
from core.resolve.review import STATE_MACHINES
from core.stores.tables import ontology_proposals, ontology_proposals_by_key

# --- proposal_key (§27.3 conventions, DR-007) --------------------------------


def test_proposal_key_is_deterministic_versioned_and_normalized() -> None:
    """Same type re-proposed in any casing/spacing = the same pool row —
    that's what stops a rejected type from re-opening review every build."""
    key = proposal_key("entity", "Spaceship")
    assert key == proposal_key("entity", "spaceship")
    assert key == proposal_key("Entity", "  Spaceship  ")
    assert key.startswith(f"fpv{FINGERPRINT_VERSION}:")


def test_proposal_key_separates_kinds_and_names() -> None:
    """An entity type and a relation type may share a name without being one
    proposal; different names are different proposals."""
    assert proposal_key("entity", "Pilot") != proposal_key("relation", "Pilot")
    assert proposal_key("entity", "Pilot") != proposal_key("entity", "Pilots")
    # length-prefixed parts: no separator smuggling
    assert proposal_key("a|b", "c") != proposal_key("a", "b|c")


# --- table shape (DESIGN §4 / §6 / §17) ---------------------------------------


def _checks(table: sa.Table) -> dict[str, str]:
    return {
        c.name: str(c.sqltext)
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint) and isinstance(c.name, str)
    }


def test_ontology_proposals_shape_and_identity() -> None:
    """NOT build-scoped (review artifact): no build_id column, and the unique
    identity is (project, proposal_key) — one pool row per type per project."""
    cols = {c.name for c in ontology_proposals.columns}
    assert "build_id" not in cols
    assert {
        "project",
        "kind",
        "type_name",
        "proposal_key",
        "fingerprint_version",
        "status",
        "decided_by",
        "decided_at",
    } <= cols
    assert ontology_proposals_by_key.unique
    assert [c.name for c in ontology_proposals_by_key.columns] == ["project", "proposal_key"]


def test_ontology_proposals_value_domains_are_checked() -> None:
    """Identifiers ban '', vocabularies are closed (entity|relation kinds),
    and the decision-fields CHECK spells out BOTH complete branches — the
    weak `(status='proposed') = (both NULL)` form accepted anonymous and
    timeless decided rows (only one field null); the behavioral proof of all
    four corners is the integration test, this pins the strong shape."""
    checks = _checks(ontology_proposals)
    assert "project <> ''" in checks.values()
    assert "type_name <> ''" in checks.values()
    assert "proposal_key <> ''" in checks.values()
    assert any("kind IN ('entity','relation')" in c for c in checks.values())
    iff = checks["ontology_proposals_decision_fields_iff_decided"]
    assert "decided_by IS NULL AND decided_at IS NULL" in iff
    assert "decided_by IS NOT NULL AND decided_at IS NOT NULL" in iff


def test_status_vocabulary_is_in_lockstep_with_the_state_machine() -> None:
    """The table CHECK and §17's ontology_proposal machine must name the SAME
    states, both ways — a state the machine can reach but the DB refuses (or
    vice versa) strands transitions. Derived from the machine, not retyped."""
    machine = STATE_MACHINES["ontology_proposal"]
    machine_states = set(machine) | {t for targets in machine.values() for t in targets}
    check = _checks(ontology_proposals)["ontology_proposals_status_valid"]
    for state in machine_states:
        assert f"'{state}'" in check  # every machine state is storable
    check_states = set(re.findall(r"'([a-z_]+)'", check))
    assert check_states == machine_states  # and nothing extra is storable


# --- persist_proposals policy gate ---------------------------------------------


async def test_unknown_policy_fails_loudly_before_any_write() -> None:
    """🔧 proposal_policy is a closed vocabulary: a typo ('automatic') must
    raise, not silently behave like 'review'."""

    class _NeverConn:
        async def execute(self, *_: object) -> None:
            raise AssertionError("must not reach the database")

    with pytest.raises(ValueError, match="proposal_policy"):
        await persist_proposals(
            _NeverConn(),  # type: ignore[arg-type]
            "proj",
            [TypeProposal("entity", "Spaceship", "Rocinante", "chunk:x:0")],
            policy="automatic",
        )
    assert PROPOSAL_POLICIES == ("review", "auto")


async def test_blank_project_is_refused() -> None:
    class _NeverConn:
        async def execute(self, *_: object) -> None:
            raise AssertionError("must not reach the database")

    with pytest.raises(ValueError, match="project"):
        await persist_proposals(
            _NeverConn(),  # type: ignore[arg-type]
            "  ",
            [],
        )
