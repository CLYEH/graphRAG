"""Why: the registry's PATCH semantics hinge on one subtle distinction — an
OMITTED field must be left untouched, but a field explicitly set to null must
be written as null. That is pure logic (the _UNSET sentinel), so it is pinned
here without a DB; the live round-trip is proved in the integration suite. The
domain errors carry the offending name for the router to surface.
"""

from __future__ import annotations

from core.registry import ProjectExistsError, ProjectNotFoundError
from core.registry.store import _UNSET, _patch_values


def test_patch_omits_unset_but_keeps_explicit_null() -> None:
    # nothing passed → empty SET clause (a no-op read, never a blind wipe)
    assert _patch_values(_UNSET, _UNSET, _UNSET) == {}
    # explicit None must be PRESENT in the SET (→ column cleared), not dropped
    assert _patch_values(None, _UNSET, _UNSET) == {"display_name": None}
    # a passed value and a passed null coexist; the omitted one stays out
    assert _patch_values("Name", None, _UNSET) == {
        "display_name": "Name",
        "description": None,
    }
    # config replaces wholesale when passed
    assert _patch_values(_UNSET, _UNSET, {"k": "v"}) == {"config": {"k": "v"}}


def test_domain_errors_carry_the_name() -> None:
    exists = ProjectExistsError("demo")
    assert exists.name == "demo"
    assert "demo" in str(exists)
    missing = ProjectNotFoundError("gone")
    assert missing.name == "gone"
    assert "gone" in str(missing)
