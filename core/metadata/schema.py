"""Document metadata schema + exposure allowlist (DR-010, UXC1b).

Two ``projects.config`` blocks and the envelope they govern:

* ``metadata_schema`` — the per-project TYPING of the ``context.attributes`` bag.
  A project declares each attribute's ``type`` and whether it is ``required``;
  an upload's ``context.attributes`` is validated against it at capture time, so
  the schema is LOAD-BEARING (an undeclared attribute, a wrong-typed value, or a
  missing required attribute fails loud) rather than decorative. Keeping every
  project-defined field under ``attributes`` (not as a new top-level key) is what
  keeps the envelope domain-agnostic — no global field enum (DR-010 rule 2).

* ``metadata_exposure`` — the allowlist of envelope field paths an agent may see
  in ``source_ref.metadata`` on read. FAIL-CLOSED: a field not listed is never
  exposed, so a sensitive ``governance`` value is never leaked merely because it
  lives in JSONB (DR-010 rule 7). This mirrors the ``query_policy`` precedent
  (a config-level policy whose shape is fixed and whose enforcement is at
  runtime) — a per-project dynamic allowlist cannot be expressed statically in
  the frozen OpenAPI response type, so the projection lives here with its own
  tests.

Both blocks follow the ``core.builds.config`` loader discipline: pull each block
out of the untrusted JSON by fixed key, type-check every leaf, reject unknown
keys in the CLOSED sub-blocks (a typo on an optional key must fail loud, not
silently disable a field), and raise ONE :class:`MetadataConfigError` so callers
catch a single type. Neither block is a frozen contract (``projects.config`` is
internal, no shared-schema surface parses it) — they evolve additively via code
review, DR-002 untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The envelope shape version, stamped server-side into every stored envelope's
#: ``schema_version`` (DR-010: the envelope is defined in openapi.yaml components;
#: this is its own version, distinct from mcp_response.schema.json's).
ENVELOPE_SCHEMA_VERSION = "1.0"

#: Attribute value types a project metadata schema can declare. Deliberately
#: small (the JSON scalars the pipeline needs) — extend additively when a real
#: project needs more; a downstream stage owns display/filterable typing.
_ATTR_TYPES = ("string", "number", "boolean")

#: The envelope's four namespaces — the only legal first segment of an exposure
#: path. Validated so a typo (``contxt.title``) fails loud instead of silently
#: exposing nothing (a decorative allowlist entry — the Class-24 trap).
_ENVELOPE_NAMESPACES = ("schema_version", "system", "context", "governance")


class MetadataConfigError(ValueError):
    """A ``projects.config`` metadata block is malformed — a shape/leaf-type
    error or an out-of-vocabulary value. One type so the caller catches once.
    Raised when LOADING project config (a project misconfiguration), distinct
    from :class:`MetadataValidationError` (a client's upload input)."""


class MetadataValidationError(ValueError):
    """An upload's ``context`` does not match the project's ``metadata_schema``
    (undeclared attribute, wrong type, or a missing required attribute). A
    CLIENT error — the upload boundary maps it to a 400."""


# --- leaf-type helpers (bool is an int subclass in Python; reject it) --------


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MetadataConfigError(f"{path} must be an object, got {type(value).__name__}")
    return value


def _str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise MetadataConfigError(f"{path} must be a string, got {type(value).__name__}")
    return value


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise MetadataConfigError(f"{path} must be a boolean, got {type(value).__name__}")
    return value


def _reject_unknown(block: Mapping[str, Any], allowed: set[str], path: str) -> None:
    extra = sorted(set(block) - allowed)
    if extra:
        raise MetadataConfigError(f"{path} has unknown key(s) {extra}; allowed: {sorted(allowed)}")


# --- metadata_schema ---------------------------------------------------------


@dataclass(frozen=True)
class AttributeDef:
    """One declared ``context.attributes`` field: its value ``type`` and whether
    an upload MUST supply it."""

    name: str
    type: str
    required: bool


@dataclass(frozen=True)
class MetadataSchema:
    """The per-project typing of the ``context.attributes`` bag (DR-010 rule 2).

    An empty schema (no attributes declared) is valid and means "this project
    defines no attributes" — an upload may then set only the core context
    (``title``/``document_type``) and governance; any attribute is undeclared
    and rejected, so the schema stays load-bearing even when empty."""

    attributes: Mapping[str, AttributeDef]

    def validate_context(self, context: Mapping[str, Any]) -> None:
        """Validate an upload's ``context`` against this schema.

        Only the ``attributes`` bag is schema-governed; ``title``/
        ``document_type`` are the fixed core (their string|null shape is enforced
        at the API boundary). Raises :class:`MetadataValidationError` on an
        undeclared attribute, a wrong-typed value, or a missing required
        attribute — every failure loud, none silently dropped."""
        attributes = context.get("attributes") or {}
        if not isinstance(attributes, Mapping):
            raise MetadataValidationError(
                f"context.attributes must be an object, got {type(attributes).__name__}"
            )
        for key, value in attributes.items():
            defn = self.attributes.get(key)
            if defn is None:
                raise MetadataValidationError(
                    f"attribute {key!r} is not declared in the project metadata_schema "
                    f"(declared: {sorted(self.attributes)})"
                )
            if not _value_matches(value, defn.type):
                raise MetadataValidationError(
                    f"attribute {key!r} must be {defn.type}, got {type(value).__name__}"
                )
        for name, defn in self.attributes.items():
            if defn.required and name not in attributes:
                raise MetadataValidationError(
                    f"required attribute {name!r} is missing from context.attributes"
                )


def _value_matches(value: Any, declared: str) -> bool:
    if declared == "string":
        return isinstance(value, str)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    # unreachable: declared type was vocabulary-checked at load time
    raise MetadataConfigError(f"unknown declared attribute type {declared!r}")


def load_metadata_schema(config: Mapping[str, Any]) -> MetadataSchema:
    """Parse ``projects.config.metadata_schema`` into a typed schema.

    An absent block yields an empty schema (no attributes). A present block is
    ``{"attributes": {<name>: {"type": <type>, "required": <bool>}}}`` — the
    attribute-name keys are DATA (any name), but each definition block is CLOSED
    (unknown keys rejected). Raises :class:`MetadataConfigError` on any malformed
    shape/type/vocabulary."""
    if "metadata_schema" not in config:
        return MetadataSchema(attributes={})
    block = _mapping(config["metadata_schema"], "metadata_schema")
    _reject_unknown(block, {"attributes"}, "metadata_schema")
    raw_attributes = _mapping(block.get("attributes", {}), "metadata_schema.attributes")
    attributes: dict[str, AttributeDef] = {}
    for name, raw in raw_attributes.items():
        path = f"metadata_schema.attributes.{name}"
        defn = _mapping(raw, path)
        # `display`/`filterable` are contract-documented attribute keys (openapi.yaml
        # DocumentMetadataContext: "keys' type/required/display/filterable are defined
        # by projects.config.metadata_schema"). They are FE-facing hints with no backend
        # ingest/validation semantics yet, so they are ALLOWED (a valid v1.2 config must
        # not be rejected at the upload boundary) but not parsed into AttributeDef — the
        # loader only needs type/required. Rejecting them would make a documented config
        # unusable; consuming them (surfacing to the Console) is a separate FE concern.
        _reject_unknown(defn, {"type", "required", "display", "filterable"}, path)
        if "type" not in defn:
            raise MetadataConfigError(f"{path}.type is required")
        attr_type = _str(defn["type"], f"{path}.type")
        if attr_type not in _ATTR_TYPES:
            raise MetadataConfigError(
                f"{path}.type must be one of {list(_ATTR_TYPES)}, got {attr_type!r}"
            )
        required = _bool(defn["required"], f"{path}.required") if "required" in defn else False
        attributes[name] = AttributeDef(name=name, type=attr_type, required=required)
    return MetadataSchema(attributes=attributes)


# --- metadata_exposure -------------------------------------------------------


@dataclass(frozen=True)
class MetadataExposure:
    """The allowlist of envelope field paths exposed to agents on read.

    FAIL-CLOSED (DR-010 rule 7): :meth:`project` returns ONLY the listed paths,
    so an unlisted ``governance``/``system`` field is never leaked. An empty
    allowlist (the default when no block is configured) exposes NOTHING."""

    fields: tuple[str, ...]

    def project(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Project a stored envelope down to only the allowlisted paths.

        Preserves the envelope's nested shape (a path ``context.title`` places
        the value at ``out["context"]["title"]``); a path may name a leaf or a
        whole sub-object, and a path absent from this envelope is simply skipped
        (an empty branch is never emitted). The result is what reaches
        ``source_ref.metadata`` — nothing else from the envelope escapes."""
        out: dict[str, Any] = {}
        for path in self.fields:
            segments = path.split(".")
            value, found = _resolve_path(envelope, segments)
            if found:
                _set_path(out, segments, value)
        return out


def _resolve_path(node: Any, segments: list[str]) -> tuple[Any, bool]:
    for segment in segments:
        if not isinstance(node, Mapping) or segment not in node:
            return None, False
        node = node[segment]
    return node, True


def _set_path(root: dict[str, Any], segments: list[str], value: Any) -> None:
    node = root
    for segment in segments[:-1]:
        branch = node.get(segment)
        if not isinstance(branch, dict):
            branch = {}
            node[segment] = branch
        node = branch
    node[segments[-1]] = value


def load_metadata_exposure(config: Mapping[str, Any]) -> MetadataExposure:
    """Parse ``projects.config.metadata_exposure`` into a typed allowlist.

    An absent block yields the empty (fail-closed) allowlist. A present block is
    ``{"fields": ["context.title", "context.attributes.case_number", ...]}`` —
    each a non-empty dotted path whose first segment is an envelope namespace
    (``system``/``context``/``governance``/``schema_version``); a path naming an
    unknown namespace is rejected (a typo would silently expose nothing — the
    decorative-allowlist trap). Raises :class:`MetadataConfigError` on any
    malformed shape."""
    if "metadata_exposure" not in config:
        return MetadataExposure(fields=())
    block = _mapping(config["metadata_exposure"], "metadata_exposure")
    _reject_unknown(block, {"fields"}, "metadata_exposure")
    raw_fields = block.get("fields", [])
    if not isinstance(raw_fields, list):
        raise MetadataConfigError(
            f"metadata_exposure.fields must be an array, got {type(raw_fields).__name__}"
        )
    fields: list[str] = []
    for i, raw in enumerate(raw_fields):
        path = _str(raw, f"metadata_exposure.fields[{i}]")
        segments = path.split(".")
        if not path or any(not segment for segment in segments):
            raise MetadataConfigError(
                f"metadata_exposure.fields[{i}] must be a non-empty dotted path, got {path!r}"
            )
        if segments[0] not in _ENVELOPE_NAMESPACES:
            raise MetadataConfigError(
                f"metadata_exposure.fields[{i}] must start with an envelope namespace "
                f"{list(_ENVELOPE_NAMESPACES)}, got {segments[0]!r}"
            )
        fields.append(path)
    return MetadataExposure(fields=tuple(fields))


# --- envelope construction ---------------------------------------------------


def build_envelope(
    *,
    connector: str,
    original_filename: str | None,
    context: Mapping[str, Any] | None,
    governance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Assemble a stored :class:`DocumentMetadataEnvelope` at capture time.

    ``system`` is stamped server-side (the connector name + the original
    filename) — never taken from client input (DR-010 rule 1/4), so human input
    can't overwrite the server-owned namespace. ``context`` is normalized to the
    fixed core shape (``title``/``document_type``/``attributes`` always present)
    so downstream reads a predictable structure; the caller has already validated
    ``context.attributes`` against the project schema."""
    raw_context = context or {}
    normalized_context = {
        "title": raw_context.get("title"),
        "document_type": raw_context.get("document_type"),
        "attributes": dict(raw_context.get("attributes") or {}),
    }
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "system": {"connector": connector, "original_filename": original_filename},
        "context": normalized_context,
        "governance": dict(governance or {}),
    }
