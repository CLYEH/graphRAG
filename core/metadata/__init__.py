"""Document metadata envelope, per-project schema, and exposure allowlist (DR-010).

The stable-core envelope ``{schema_version, system, context, governance}`` is a
domain-agnostic document-context shape: ``system`` is server-stamped, ``context``
holds a fixed core (``title``/``document_type``) plus a project-defined
``attributes`` bag typed by :class:`MetadataSchema`, and ``governance`` is gated
out of agent-visible output unless a field is named in :class:`MetadataExposure`
(fail-closed — presence in storage is not exposure, DR-010 rule 7).
"""

from __future__ import annotations

from core.metadata.schema import (
    ENVELOPE_SCHEMA_VERSION,
    AttributeDef,
    MetadataConfigError,
    MetadataExposure,
    MetadataSchema,
    MetadataValidationError,
    build_envelope,
    load_metadata_exposure,
    load_metadata_schema,
)

__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "AttributeDef",
    "MetadataConfigError",
    "MetadataExposure",
    "MetadataSchema",
    "MetadataValidationError",
    "build_envelope",
    "load_metadata_exposure",
    "load_metadata_schema",
]
