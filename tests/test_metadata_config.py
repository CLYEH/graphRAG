"""Why: the metadata ``schema``/``exposure`` blocks are the two guarantees that
make the DR-010 envelope safe and general. ``metadata_schema`` must be
LOAD-BEARING — an undeclared attribute, a wrong-typed value, or a missing
required attribute has to fail loud, else the "schema" is decorative and any
JSON slips through (a project-defined field bag with no discipline). And
``metadata_exposure`` must be FAIL-CLOSED — a governance/system field is exposed
to agents ONLY when its path is explicitly allowlisted, never by mere presence
in storage (DR-010 rule 7); a typo'd path must fail loud rather than silently
exposing nothing. These tests pin both, plus the loader discipline shared with
``core.builds.config`` (absent block → safe default; closed sub-blocks reject
unknown keys; bool is not a number).
"""

from __future__ import annotations

import pytest

from core.metadata.schema import (
    ENVELOPE_SCHEMA_VERSION,
    MetadataConfigError,
    MetadataValidationError,
    build_envelope,
    load_metadata_exposure,
    load_metadata_schema,
)

# --- metadata_schema loading -------------------------------------------------


def test_absent_schema_is_empty_not_an_error() -> None:
    schema = load_metadata_schema({})
    assert schema.attributes == {}
    # an empty schema still governs: no attribute is declared, so any is rejected
    schema.validate_context({"title": "x"})  # core-only context is fine
    with pytest.raises(MetadataValidationError):
        schema.validate_context({"attributes": {"anything": "v"}})


def test_schema_parses_attribute_types_and_required() -> None:
    schema = load_metadata_schema(
        {
            "metadata_schema": {
                "attributes": {
                    "case_number": {"type": "string", "required": True},
                    "year": {"type": "number", "required": False},
                    "sealed": {"type": "boolean"},
                }
            }
        }
    )
    assert schema.attributes["case_number"].required is True
    assert schema.attributes["year"].type == "number"
    # required defaults to False when omitted
    assert schema.attributes["sealed"].required is False


def test_unknown_attribute_type_fails_loud() -> None:
    with pytest.raises(MetadataConfigError, match="type must be one of"):
        load_metadata_schema({"metadata_schema": {"attributes": {"x": {"type": "date"}}}})


def test_attribute_definition_rejects_unknown_key() -> None:
    # a typo on an optional key (``require`` for ``required``) must fail loud,
    # not silently make the attribute optional (the closed-sub-block discipline)
    with pytest.raises(MetadataConfigError, match="unknown key"):
        load_metadata_schema(
            {"metadata_schema": {"attributes": {"x": {"type": "string", "require": True}}}}
        )


def test_attribute_definition_allows_contract_documented_display_and_filterable() -> None:
    # WHY: the frozen contract (openapi.yaml DocumentMetadataContext) documents
    # ``type/required/display/filterable`` as the metadata_schema attribute keys, so a
    # valid v1.2 config carrying display/filterable must NOT be rejected at the upload
    # boundary. They are FE-facing hints with no backend semantics yet — allowed but not
    # parsed into AttributeDef (the loader still only reads type/required). Revert-probe:
    # under the old {"type","required"} closed set this raised "unknown key".
    schema = load_metadata_schema(
        {
            "metadata_schema": {
                "attributes": {
                    "case_number": {
                        "type": "string",
                        "required": True,
                        "display": "Case number",
                        "filterable": True,
                    }
                }
            }
        }
    )
    # the documented config loads, and the backend-relevant fields are still parsed…
    assert schema.attributes["case_number"].type == "string"
    assert schema.attributes["case_number"].required is True
    # …while a genuine typo still fails loud (the closed set is only widened by the
    # two documented keys, not opened up).
    with pytest.raises(MetadataConfigError, match="unknown key"):
        load_metadata_schema(
            {"metadata_schema": {"attributes": {"x": {"type": "string", "displ": True}}}}
        )


def test_schema_rejects_missing_type() -> None:
    with pytest.raises(MetadataConfigError, match="type is required"):
        load_metadata_schema({"metadata_schema": {"attributes": {"x": {"required": True}}}})


# --- metadata_schema validation (the load-bearing part) ----------------------


def test_undeclared_attribute_is_rejected() -> None:
    schema = load_metadata_schema(
        {"metadata_schema": {"attributes": {"case_number": {"type": "string"}}}}
    )
    with pytest.raises(MetadataValidationError, match="not declared"):
        schema.validate_context({"attributes": {"case_number": "42", "leak": "x"}})


def test_wrong_typed_attribute_is_rejected() -> None:
    schema = load_metadata_schema({"metadata_schema": {"attributes": {"year": {"type": "number"}}}})
    with pytest.raises(MetadataValidationError, match="must be number"):
        schema.validate_context({"attributes": {"year": "2026"}})


def test_boolean_is_not_a_number_attribute() -> None:
    # bool <: int in Python — a boolean must not satisfy a number attribute
    schema = load_metadata_schema({"metadata_schema": {"attributes": {"year": {"type": "number"}}}})
    with pytest.raises(MetadataValidationError):
        schema.validate_context({"attributes": {"year": True}})


def test_missing_required_attribute_is_rejected() -> None:
    schema = load_metadata_schema(
        {"metadata_schema": {"attributes": {"case_number": {"type": "string", "required": True}}}}
    )
    with pytest.raises(MetadataValidationError, match="required attribute"):
        schema.validate_context({"title": "x", "attributes": {}})


def test_valid_context_passes() -> None:
    schema = load_metadata_schema(
        {
            "metadata_schema": {
                "attributes": {
                    "case_number": {"type": "string", "required": True},
                    "year": {"type": "number"},
                }
            }
        }
    )
    schema.validate_context(
        {
            "title": "Doc",
            "document_type": "ruling",
            "attributes": {"case_number": "42", "year": 2026},
        }
    )


# --- metadata_exposure (fail-closed allowlist) -------------------------------

_ENVELOPE = {
    "schema_version": "1.0",
    "system": {"connector": "upload", "original_filename": "case.txt"},
    "context": {
        "title": "Ruling 42",
        "document_type": "ruling",
        "attributes": {"case_number": "42"},
    },
    "governance": {"visibility": "restricted", "classification": "secret"},
}


def test_absent_exposure_exposes_nothing() -> None:
    exposure = load_metadata_exposure({})
    assert exposure.fields == ()
    assert exposure.project(_ENVELOPE) == {}


def test_exposure_projects_only_allowlisted_paths() -> None:
    exposure = load_metadata_exposure(
        {"metadata_exposure": {"fields": ["context.title", "context.attributes.case_number"]}}
    )
    projected = exposure.project(_ENVELOPE)
    assert projected == {"context": {"title": "Ruling 42", "attributes": {"case_number": "42"}}}
    # governance and system are NOT leaked despite being present in storage
    assert "governance" not in projected
    assert "system" not in projected


def test_exposure_can_name_a_whole_subobject() -> None:
    exposure = load_metadata_exposure({"metadata_exposure": {"fields": ["governance"]}})
    assert exposure.project(_ENVELOPE) == {"governance": _ENVELOPE["governance"]}


def test_exposure_skips_paths_absent_from_this_envelope() -> None:
    exposure = load_metadata_exposure(
        {"metadata_exposure": {"fields": ["context.attributes.missing", "context.title"]}}
    )
    # a path not present in the envelope is simply not emitted (no empty branch)
    assert exposure.project(_ENVELOPE) == {"context": {"title": "Ruling 42"}}


def test_exposure_typo_in_namespace_fails_loud() -> None:
    # a path whose first segment is not an envelope namespace would silently
    # expose nothing — the decorative-allowlist trap — so it must fail loud
    with pytest.raises(MetadataConfigError, match="envelope namespace"):
        load_metadata_exposure({"metadata_exposure": {"fields": ["contxt.title"]}})


def test_exposure_rejects_empty_path_segment() -> None:
    with pytest.raises(MetadataConfigError, match="non-empty dotted path"):
        load_metadata_exposure({"metadata_exposure": {"fields": ["context..title"]}})


def test_exposure_rejects_unknown_block_key() -> None:
    with pytest.raises(MetadataConfigError, match="unknown key"):
        load_metadata_exposure({"metadata_exposure": {"field": ["context.title"]}})


# --- envelope construction ---------------------------------------------------


def test_build_envelope_stamps_system_and_normalizes_context() -> None:
    envelope = build_envelope(
        connector="upload",
        original_filename="Case 42.txt",
        context={"title": "Ruling", "attributes": {"case_number": "42"}},
        governance={"visibility": "restricted"},
    )
    assert envelope["schema_version"] == ENVELOPE_SCHEMA_VERSION
    assert envelope["system"] == {"connector": "upload", "original_filename": "Case 42.txt"}
    # context normalized to the fixed core shape (document_type present as null)
    assert envelope["context"] == {
        "title": "Ruling",
        "document_type": None,
        "attributes": {"case_number": "42"},
    }
    assert envelope["governance"] == {"visibility": "restricted"}


def test_build_envelope_defaults_empty_context_and_governance() -> None:
    envelope = build_envelope(
        connector="upload", original_filename=None, context=None, governance=None
    )
    assert envelope["context"] == {"title": None, "document_type": None, "attributes": {}}
    assert envelope["governance"] == {}
    assert envelope["system"]["original_filename"] is None
